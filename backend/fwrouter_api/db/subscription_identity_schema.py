"""Additive durable identity intent; deliberately independent of application imports."""

IDENTITY_DDL = (
    """CREATE TABLE IF NOT EXISTS subscription_node_identities (
        identity_id TEXT PRIMARY KEY,
        origin_server_id TEXT NOT NULL UNIQUE,
        current_server_id TEXT NOT NULL UNIQUE,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        provenance_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS subscription_node_aliases (
        server_id TEXT PRIMARY KEY,
        identity_id TEXT NOT NULL,
        FOREIGN KEY (identity_id) REFERENCES subscription_node_identities(identity_id)
    )""",
    """CREATE TABLE IF NOT EXISTS subscription_node_credentials (
        scope TEXT NOT NULL,
        identity_id TEXT NOT NULL,
        client_uuid TEXT NOT NULL UNIQUE,
        client_email TEXT NOT NULL UNIQUE,
        epoch INTEGER NOT NULL DEFAULT 1 CHECK (epoch > 0),
        revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
        PRIMARY KEY (scope, identity_id),
        FOREIGN KEY (identity_id) REFERENCES subscription_node_identities(identity_id)
    )""",
    """CREATE TABLE IF NOT EXISTS subscription_node_identity_transitions (
        identity_id TEXT NOT NULL,
        from_server_id TEXT NOT NULL,
        to_server_id TEXT NOT NULL,
        from_revision INTEGER NOT NULL CHECK (from_revision > 0),
        to_revision INTEGER NOT NULL CHECK (to_revision = from_revision + 1),
        before_provenance_sha256 TEXT NOT NULL,
        after_provenance_sha256 TEXT NOT NULL,
        source_ownership_sha256 TEXT NOT NULL,
        operator_plan_sha256 TEXT,
        checkpoint_sha256 TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (identity_id, from_revision),
        UNIQUE (from_server_id, to_server_id, from_revision),
        FOREIGN KEY (identity_id) REFERENCES subscription_node_identities(identity_id)
    )""",
    """CREATE TABLE IF NOT EXISTS subscription_node_credential_history (
        client_uuid TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        identity_id TEXT NOT NULL,
        epoch INTEGER NOT NULL CHECK (epoch > 0),
        operation_id_sha256 TEXT NOT NULL,
        retired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (identity_id) REFERENCES subscription_node_identities(identity_id)
    )""",
)


def create_identity_schema(connection):
    # execute(), rather than executescript(), preserves the caller's transaction.
    for statement in IDENTITY_DDL:
        connection.execute(statement)
