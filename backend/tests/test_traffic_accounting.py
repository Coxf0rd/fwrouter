from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
import os
import subprocess
from pathlib import Path

from fwrouter_api.jobs.extended_handlers import traffic_accounting_collect_handler
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.traffic import (
    cleanup_traffic_history,
    collect_traffic_from_script,
    get_traffic_accounting_state,
    list_monthly_traffic,
    record_traffic_samples,
)


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()


def _seed_subject(subject_id: str) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                desired_mode,
                runtime_state,
                is_active
            )
            VALUES (?, 'lan', ?, ?, 'global', 'active', 1)
            """,
            (subject_id, subject_id, subject_id),
        )


def test_record_traffic_samples_aggregates_monthly_deltas(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-1")

    baseline = record_traffic_samples(
        [
            {
                "counter_key": "lan-1:direct",
                "subject_id": "lan-1",
                "path": "direct",
                "rx_bytes": 1000,
                "tx_bytes": 500,
            }
        ],
        collector="pytest",
        dry_run=False,
    )
    assert baseline["ok"] is True
    assert baseline["seeded_count"] == 1
    assert baseline["total_rx_delta"] == 0
    assert baseline["total_tx_delta"] == 0

    second = record_traffic_samples(
        [
            {
                "counter_key": "lan-1:direct",
                "subject_id": "lan-1",
                "path": "direct",
                "rx_bytes": 1300,
                "tx_bytes": 700,
            }
        ],
        collector="pytest",
        dry_run=False,
    )
    assert second["ok"] is True
    assert second["updated_count"] == 1
    assert second["total_rx_delta"] == 300
    assert second["total_tx_delta"] == 200

    rows = list_monthly_traffic(subject_id="lan-1")
    assert len(rows) == 1
    assert rows[0]["direct_rx_bytes"] == 300
    assert rows[0]["direct_tx_bytes"] == 200
    assert rows[0]["total_direct_bytes"] == 500


def test_collect_traffic_from_script_reads_json_payload(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-2")

    class _FakeScriptResult:
        def __init__(self) -> None:
            self.ok = True
            self.stdout = json.dumps(
                {
                    "counters": [
                        {
                            "counter_key": "lan-2:vpn",
                            "subject_id": "lan-2",
                            "path": "vpn",
                            "rx_bytes": 50,
                            "tx_bytes": 10,
                        }
                    ]
                }
            )
            self.stderr = ""

        def to_dict(self) -> dict[str, object]:
            return {"ok": True}

    monkeypatch.setattr(
        "fwrouter_api.services.traffic.DEFAULT_SCRIPT_RUNNER.run",
        lambda script_id, extra_args=None: _FakeScriptResult(),
    )

    result = collect_traffic_from_script(dry_run=False, collector="pytest-script")

    assert result["ok"] is True
    assert result["script_id"] == "traffic_collect"
    assert result["processed_count"] == 1


def test_traffic_collect_job_result_is_compact(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-compact")

    class _FakeScriptResult:
        ok = True
        stdout = json.dumps(
            {
                "counters": [
                    {
                        "counter_key": "lan-compact:vpn",
                        "subject_id": "lan-compact",
                        "path": "vpn",
                        "rx_bytes": 50,
                        "tx_bytes": 10,
                    }
                ]
            }
        )
        stderr = ""

        def to_dict(self) -> dict[str, object]:
            return {
                "script_id": "traffic_collect",
                "returncode": 0,
                "stdout": self.stdout,
                "stderr": self.stderr,
                "duration_seconds": 0.01,
                "ok": True,
            }

    monkeypatch.setattr(
        "fwrouter_api.services.traffic.DEFAULT_SCRIPT_RUNNER.run",
        lambda script_id, extra_args=None: _FakeScriptResult(),
    )

    result = traffic_accounting_collect_handler(
        {
            "job_id": "job-compact",
            "input": {
                "collector": "pytest-script",
                "dry_run": False,
                "use_script": True,
                "script_id": "traffic_collect",
            },
        }
    )

    traffic = result["traffic"]
    assert traffic["processed_count"] == 1
    assert "processed" not in traffic
    assert "stdout" not in traffic["script_result"]
    assert traffic["script_result"]["returncode"] == 0


def test_traffic_state_exposes_signal_freshness(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-state")

    baseline = get_traffic_accounting_state()
    assert baseline["default_use_script"] is True
    assert baseline["signal_authoritative"] is False

    record_traffic_samples(
        [
            {
                "counter_key": "lan-state:vpn",
                "subject_id": "lan-state",
                "path": "vpn",
                "rx_bytes": 1,
                "tx_bytes": 1,
            }
        ],
        collector="pytest",
        dry_run=False,
    )

    current = get_traffic_accounting_state()
    assert current["source"] == "traffic_collect_script"
    assert current["signal_fresh"] is True
    assert current["signal_authoritative"] is True


def test_record_traffic_samples_auto_creates_fwrouter_subject(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    result = record_traffic_samples(
        [
            {
                "counter_key": "fwrouter:global:vpn",
                "subject_id": "fwrouter:global",
                "path": "vpn",
                "rx_bytes": 10,
                "tx_bytes": 0,
            }
        ],
        collector="pytest",
        dry_run=False,
    )

    assert result["ok"] is True

    with db_session() as connection:
        subject = connection.execute(
            "SELECT subject_type, is_active FROM subjects WHERE subject_id = ?",
            ("fwrouter:global",),
        ).fetchone()
        detail = connection.execute(
            "SELECT component_name FROM subject_fwrouter WHERE subject_id = ?",
            ("fwrouter:global",),
        ).fetchone()

    assert subject is not None
    assert subject["subject_type"] == "fwrouter"
    assert subject["is_active"] == 1
    assert detail is not None
    assert detail["component_name"] == "global"


def test_record_traffic_samples_does_not_write_success_operational_log(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-logless")

    with db_session() as connection:
        before = connection.execute("SELECT COUNT(*) FROM operational_logs").fetchone()[0]

    result = record_traffic_samples(
        [
            {
                "counter_key": "lan-logless:direct",
                "subject_id": "lan-logless",
                "path": "direct",
                "rx_bytes": 10,
                "tx_bytes": 5,
            }
        ],
        collector="pytest",
        dry_run=False,
    )

    with db_session() as connection:
        after = connection.execute("SELECT COUNT(*) FROM operational_logs").fetchone()[0]

    assert result["ok"] is True
    assert after == before


def test_record_traffic_samples_maps_tailscale_named_counters_to_tailscale_node_subjects(
    monkeypatch, tmp_path: Path
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                desired_mode,
                runtime_state,
                is_active
            )
            VALUES (?, 'tailscale_node', ?, ?, 'global', 'active', 1)
            """,
            ("tailscale-node:18", "tailscale-node:18", "peer-18"),
        )

    result = record_traffic_samples(
        [
            {
                "counter_key": "nft:counter:cnt_tailscale_node_18_vpn_tx",
                "rx_bytes": 150,
                "tx_bytes": 0,
            },
            {
                "counter_key": "nft:counter:cnt_tailscale_node_18_vpn_rx",
                "rx_bytes": 300,
                "tx_bytes": 0,
            },
        ],
        collector="pytest",
        dry_run=False,
    )

    assert result["ok"] is True
    assert result["invalid_count"] == 0
    processed_by_key = {item["counter_key"]: item for item in result["processed"]}
    assert processed_by_key["nft:counter:cnt_tailscale_node_18_vpn_tx"]["subject_id"] == "tailscale-node:18"
    assert processed_by_key["nft:counter:cnt_tailscale_node_18_vpn_tx"]["tx_bytes"] == 150
    assert processed_by_key["nft:counter:cnt_tailscale_node_18_vpn_tx"]["rx_bytes"] == 0
    assert processed_by_key["nft:counter:cnt_tailscale_node_18_vpn_rx"]["subject_id"] == "tailscale-node:18"


def test_record_traffic_samples_deduplicates_repeated_invalid_sample_logs(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    payload = [
        {
            "counter_key": "nft:counter:cnt_tailscale_node_18_vpn_tx",
            "rx_bytes": 150,
            "tx_bytes": 0,
        }
    ]

    first = record_traffic_samples(payload, collector="pytest", dry_run=False)
    second = record_traffic_samples(payload, collector="pytest", dry_run=False)

    assert first["ok"] is False
    assert second["ok"] is False

    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM operational_logs
            WHERE event_type = 'traffic_accounting_collected'
            """
        ).fetchone()

    assert rows is not None
    assert rows["count"] == 1


def test_cleanup_traffic_history_removes_invalid_snapshots(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-clean")
    _seed_subject("lan-missing")

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO traffic_counter_snapshots (
                counter_key,
                subject_id,
                path,
                rx_bytes,
                tx_bytes,
                collected_at,
                metadata_json
            )
            VALUES
                ('invalid-missing', 'lan-missing', 'vpn', 1, 1, CURRENT_TIMESTAMP, json('{}')),
                ('valid-live', 'lan-clean', 'vpn', 1, 1, CURRENT_TIMESTAMP, json('{}'))
            """
        )
        connection.execute(
            """
            UPDATE subjects
            SET is_deleted = 1, updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = 'lan-missing'
            """
        )

    result = cleanup_traffic_history(dry_run=False)

    assert result["deleted_invalid_snapshots_count"] == 1

    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT counter_key
            FROM traffic_counter_snapshots
            ORDER BY counter_key
            """
        ).fetchall()

    assert [row["counter_key"] for row in rows] == ["valid-live"]


def test_traffic_collect_script_reads_global_vpn_mark_and_xray_stats(tmp_path: Path) -> None:
    config_path = tmp_path / "xray-config.json"
    config_path.write_text(
        json.dumps(
            {
                "api": {
                    "tag": "fwrouter-api",
                    "services": ["StatsService"],
                },
                "inbounds": [
                    {
                        "tag": "vless-ws",
                        "protocol": "vless",
                        "settings": {
                            "clients": [
                                {
                                    "id": "uuid-1",
                                    "email": "stats@example.test",
                                    "fwrouterBinding": {
                                        "subject_id": "xray:uuid-1",
                                    },
                                },
                                {
                                    "id": "uuid-without-binding",
                                    "email": "unbound-stats@example.test",
                                }
                            ]
                        },
                    },
                    {
                        "tag": "fwrouter-api",
                        "listen": "127.0.0.1",
                        "port": 10085,
                        "protocol": "dokodemo-door",
                        "settings": {"address": "127.0.0.1"},
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    (fake_bin / "nft").write_text(
        """#!/bin/sh
if [ "$1" = "list" ] && [ "$2" = "table" ]; then
cat <<'EOF'
table inet fwrouter_v2 {
  chain prerouting {
    counter packets 10 bytes 321 comment "global direct path"
    counter packets 20 bytes 654 comment "fwrouter vpn mark tcp:5202"
  }
}
EOF
elif [ "$1" = "-j" ] && [ "$2" = "list" ] && [ "$3" = "counters" ]; then
  printf '{"nftables":[]}'
else
  exit 1
fi
""",
        encoding="utf-8",
    )
    (fake_bin / "docker").write_text(
        """#!/bin/sh
cat <<'EOF'
{"stat":[
  {"name":"user>>>stats@example.test>>>traffic>>>downlink","value":1234},
  {"name":"user>>>stats@example.test>>>traffic>>>uplink","value":567},
  {"name":"user>>>unbound-stats@example.test>>>traffic>>>downlink","value":4321},
  {"name":"user>>>unbound-stats@example.test>>>traffic>>>uplink","value":765}
]}
EOF
""",
        encoding="utf-8",
    )
    (fake_bin / "curl").write_text(
        """#!/bin/sh
exit 1
""",
        encoding="utf-8",
    )

    for executable in ("nft", "docker", "curl"):
        path = fake_bin / executable
        path.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["FWROUTER_XRAY_CONFIG"] = str(config_path)
    env["FWROUTER_MIHOMO_CONFIG"] = str(tmp_path / "missing-mihomo.yaml")

    completed = subprocess.run(
        ["/usr/local/libexec/fwrouter/traffic-collect.sh"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    counters = {item["counter_key"]: item for item in payload["counters"]}

    assert counters["fwrouter:global:direct"]["rx_bytes"] == 321
    assert counters["fwrouter:global:vpn"]["rx_bytes"] == 654
    assert counters["xray:subject:xray:uuid-1"]["subject_id"] == "xray:uuid-1"
    assert counters["xray:subject:xray:uuid-1"]["path"] == "vpn"
    assert counters["xray:subject:xray:uuid-1"]["rx_bytes"] == 1234
    assert counters["xray:subject:xray:uuid-1"]["tx_bytes"] == 567
    assert counters["xray:subject:xray:uuid-without-binding"]["subject_id"] == "xray:uuid-without-binding"
    assert counters["xray:subject:xray:uuid-without-binding"]["path"] == "vpn"
    assert counters["xray:subject:xray:uuid-without-binding"]["rx_bytes"] == 4321
    assert counters["xray:subject:xray:uuid-without-binding"]["tx_bytes"] == 765
