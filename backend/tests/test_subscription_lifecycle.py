from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import get_db_path, initialize_database


import json
import sqlite3
from pathlib import Path
from subprocess import CompletedProcess

from fastapi.testclient import TestClient
import httpx

import fwrouter_api.adapters.subscription as subscription_adapter_module
import fwrouter_api.routes.subscription as subscription_route
import fwrouter_api.services.subscription_refresh_job as subscription_refresh_job
from fwrouter_api.adapters.subscription import (
    CLIENT_COMPATIBLE_PROFILE,
    LEGACY_FLCLASH_PROFILE,
    HttpMihomoSubscriptionAdapter,
    SubscriptionRequestProfile,
    SubscriptionRefreshResult,
    SubscriptionRefreshStatus,
    SubscriptionServer,
    detect_subscription_payload,
    parse_subscription_payload,
)
from fwrouter_api.jobs.manager import JobManager
from fwrouter_api.main import create_app
from fwrouter_api.services.jobs import JobLockConflictError, get_job, mark_job_running
from fwrouter_api.services import subscription as subscription_service
from fwrouter_api.services import subscription_pipeline as pipeline_service
from fwrouter_api.services.subscription import (
    get_subscription_state,
    normalize_subscription_urls,
    refresh_subscription_inventory_batch,
    refresh_subscription_inventory,
    save_subscription_url,
    subscription_registry_import_plan,
    validate_subscription_url,
)
from fwrouter_api.services.server_state import ensure_routing_global_state
from fwrouter_api.services.subscription_profiles import (
    list_desired_subscription_xray_clients,
    resolve_subscription_client,
)
from fwrouter_api.services.subjects import list_subjects


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_MAINTENANCE_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_WATCHDOG_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_RUNTIME_CONVERGENCE_SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()


class _FakeSubscriptionAdapter:
    def __init__(self, result: SubscriptionRefreshResult) -> None:
        self.result = result
        self.calls: list[str] = []

    def refresh(self, url: str) -> SubscriptionRefreshResult:
        self.calls.append(url)
        return self.result


class _FakeSubscriptionAdapterByUrl:
    def __init__(self, results: dict[str, SubscriptionRefreshResult]) -> None:
        self.results = results
        self.calls: list[str] = []

    def refresh(self, url: str) -> SubscriptionRefreshResult:
        self.calls.append(url)
        return self.results[url]


class _FakeHttpClient:
    calls: list[dict[str, object]] = []
    responses: list[httpx.Response] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.kwargs = kwargs

    def __enter__(self) -> "_FakeHttpClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str) -> httpx.Response:
        self.__class__.calls.append(
            {
                "url": url,
                "headers": dict(self.kwargs.get("headers") or {}),
            }
        )
        response = self.__class__.responses.pop(0)
        if response.request is None:
            response._request = httpx.Request("GET", url)
        return response


def _fake_response(
    body: str,
    *,
    content_type: str = "text/plain; charset=utf-8",
    url: str = "https://example.test/sub",
) -> httpx.Response:
    return httpx.Response(
        200,
        content=body.encode("utf-8"),
        headers={"content-type": content_type},
        request=httpx.Request("GET", url),
    )


def _install_fake_http(monkeypatch, *responses: httpx.Response) -> None:
    _FakeHttpClient.calls = []
    _FakeHttpClient.responses = list(responses)
    monkeypatch.setattr(subscription_adapter_module.httpx, "Client", _FakeHttpClient)


def _success_refresh_result(*names: str) -> SubscriptionRefreshResult:
    servers = [
        SubscriptionServer(
            server_id=name,
            server_name=name,
            provider_name="subscription",
            raw={"name": name, "type": "vless"},
        )
        for name in names
    ]
    return SubscriptionRefreshResult(
        status=SubscriptionRefreshStatus.SUCCESS,
        servers=servers,
        message="refresh ok",
        metadata={"url": "https://example.test/sub", "servers_count": len(servers)},
    )


def _failed_refresh_result() -> SubscriptionRefreshResult:
    return SubscriptionRefreshResult(
        status=SubscriptionRefreshStatus.FAILED,
        message="download failed",
        error_code="SUBSCRIPTION_DOWNLOAD_FAILED",
        error_message="download failed",
        metadata={"url": "https://example.test/sub"},
    )


def _client() -> TestClient:
    return TestClient(create_app(enable_startup_tasks=False))


class _FakeRefreshJobManager:
    def __init__(self, job: dict[str, object] | None = None) -> None:
        self.created: list[dict[str, object]] = []
        self.started: list[str] = []
        self.job = job or {
            "job_id": "job-refresh-1",
            "job_type": "subscription_refresh",
            "status": "running",
            "lock_key": "subscription_refresh",
        }

    def create(self, job_type: str, **kwargs: object) -> dict[str, object]:
        self.created.append({"job_type": job_type, **kwargs})
        return dict(self.job)

    def register_handler(self, job_type: str, handler: object) -> None:
        return None

    def start_job(self, job_id: str) -> dict[str, object]:
        self.started.append(job_id)
        return dict(self.job)


def test_subscription_detection_identifies_clash_yaml() -> None:
    detection = detect_subscription_payload(
        "proxies:\n  - name: alpha\n    type: vless\n    server: one.example\n",
        content_type="text/yaml",
    )

    assert detection.detected_format == "clash_yaml"
    assert detection.raw_entry_count == 1
    assert detection.parseable_by_current_parser is True
    assert detection.provider_placeholder is False


def test_subscription_detection_identifies_base64_uri_subscription() -> None:
    payload = "dmxlc3M6Ly91dWlkQG9uZS5leGFtcGxlOjQ0Mz9zZWN1cml0eT1yZWFsaXR5I2FscGhhCg=="

    detection = detect_subscription_payload(payload)

    assert detection.detected_format == "base64_subscription"
    assert detection.raw_entry_count == 1
    assert detection.parseable_by_current_parser is False


def test_subscription_detection_identifies_plain_uri_lines() -> None:
    detection = detect_subscription_payload(
        "vless://uuid@one.example:443?security=reality#alpha\n"
        "trojan://secret@two.example:443#beta\n"
    )

    assert detection.detected_format == "plain_uri_lines"
    assert detection.raw_entry_count == 2
    assert detection.parseable_by_current_parser is False


def test_subscription_detection_identifies_json_profile() -> None:
    detection = detect_subscription_payload(
        json.dumps({"outbounds": [{"protocol": "vless"}, {"protocol": "freedom"}]})
    )

    assert detection.detected_format == "json_profile"
    assert detection.raw_entry_count == 2
    assert detection.parseable_by_current_parser is False


def test_subscription_detection_marks_provider_placeholder() -> None:
    detection = detect_subscription_payload(
        "proxies:\n"
        "  - name: Ваше приложение не поддерживается.\n"
        "    type: direct\n"
    )

    assert detection.detected_format == "clash_yaml"
    assert detection.provider_placeholder is True
    assert detection.parseable_by_current_parser is False
    assert detection.unsupported_reason == "provider_placeholder"


def test_subscription_fetch_profiles_preserve_device_headers(monkeypatch) -> None:
    _install_fake_http(
        monkeypatch,
        _fake_response("proxies:\n  - name: alpha\n    type: vless\n"),
    )
    adapter = HttpMihomoSubscriptionAdapter()

    result = adapter.fetch_with_profile(
        "https://example.test/sub",
        request_profile=LEGACY_FLCLASH_PROFILE,
    )

    headers = _FakeHttpClient.calls[0]["headers"]
    assert result.ok is True
    assert headers["User-Agent"] == "FlClashX/1.0.0"
    assert headers["x-hwid"] == "fwrouter-v2-minis"
    assert headers["x-device-model"] == "FWRouter v2 minis"


def test_subscription_request_profiles_have_independent_headers(monkeypatch) -> None:
    _install_fake_http(
        monkeypatch,
        _fake_response("proxies:\n  - name: alpha\n    type: vless\n"),
        _fake_response("vless://uuid@one.example:443#alpha"),
    )
    adapter = HttpMihomoSubscriptionAdapter()

    adapter.fetch_with_profile("https://example.test/legacy", request_profile=LEGACY_FLCLASH_PROFILE)
    adapter.fetch_with_profile("https://example.test/full", request_profile=CLIENT_COMPATIBLE_PROFILE)

    first = _FakeHttpClient.calls[0]["headers"]
    second = _FakeHttpClient.calls[1]["headers"]
    assert first["User-Agent"] == "FlClashX/1.0.0"
    assert second["User-Agent"] == "Happ/3.19.1/Android"
    assert first["Accept"] != second["Accept"]
    assert first["x-hwid"] == second["x-hwid"] == "fwrouter-v2-minis"


def test_subscription_full_payload_detected_and_refresh_parses_uri(monkeypatch) -> None:
    _install_fake_http(
        monkeypatch,
        _fake_response("vless://uuid@one.example:443#alpha"),
        _fake_response("vless://uuid@one.example:443#alpha"),
        _fake_response("vless://uuid@one.example:443#alpha"),
    )
    adapter = HttpMihomoSubscriptionAdapter()

    fetched = adapter.fetch_with_profile(
        "https://example.test/full",
        request_profile=CLIENT_COMPATIBLE_PROFILE,
    )
    refreshed = adapter.refresh("https://example.test/full")

    assert fetched.ok is True
    assert fetched.metadata["detected_format"] == "plain_uri_lines"
    assert fetched.metadata["parseable_by_current_parser"] is False
    assert refreshed.ok is True
    assert refreshed.servers[0].server_name == "alpha"


def test_subscription_fetch_diagnostics_do_not_include_credentials(monkeypatch) -> None:
    secret_uuid = "d0414af1-f955-4c10-b8ed-8bfe6db952d7"
    secret_key = "very-secret-public-key"
    _install_fake_http(
        monkeypatch,
        _fake_response(
            f"vless://{secret_uuid}@one.example:443?pbk={secret_key}&sid=abc#alpha"
        ),
    )
    adapter = HttpMihomoSubscriptionAdapter()

    result = adapter.fetch_with_profile(
        "https://example.test/sub",
        request_profile=CLIENT_COMPATIBLE_PROFILE,
    )

    diagnostics = json.dumps(result.metadata, ensure_ascii=False)
    assert result.ok is True
    assert secret_uuid not in diagnostics
    assert secret_key not in diagnostics


def test_subscription_parse_base64_uri_subscription() -> None:
    uri = "vless://uuid-a@one.example:443?type=tcp&security=reality#alpha"
    import base64

    result = parse_subscription_payload(base64.b64encode(f"{uri}\n".encode()).decode())

    assert result.ok is True
    assert result.metadata["detected_format"] == "base64_subscription"
    assert result.servers[0].server_id.startswith("sub:")
    assert result.servers[0].server_name == "alpha"
    assert result.servers[0].host == "one.example"


def test_subscription_parse_plain_uri_subscription() -> None:
    result = parse_subscription_payload(
        "vless://uuid-a@one.example:443?type=tcp&security=reality#alpha\n"
    )

    assert result.ok is True
    assert result.metadata["detected_format"] == "plain_uri_lines"
    assert result.servers[0].protocol == "vless"


def test_subscription_parse_vless_reality_xhttp_regression() -> None:
    uri = (
        "vless://uuid-a@my.crushboy.net:443?"
        "encryption=none&type=xhttp&path=%2Fb9ecd35b28fe&mode=stream-one"
        "&security=reality&sni=my.crushboy.net&fp=random&pbk=public-key&sid=short-id"
        "#🇩🇪Auto%20Server🔋%20-%20NEW"
    )

    result = parse_subscription_payload(uri)
    server = result.servers[0]

    assert result.ok is True
    assert server.server_name == "🇩🇪Auto Server🔋 - NEW"
    assert server.protocol == "vless"
    assert server.host == "my.crushboy.net"
    assert server.port == 443
    assert server.transport == "xhttp"
    assert server.raw["security"] == "reality"
    assert server.raw["xhttp-opts"]["mode"] == "stream-one"


def test_subscription_same_display_name_different_links_are_two_servers() -> None:
    result = parse_subscription_payload(
        "vless://uuid-a@one.example:443?type=tcp#same\n"
        "vless://uuid-b@two.example:443?type=tcp#same\n"
    )

    assert result.ok is True
    assert len(result.servers) == 2
    assert len({server.server_id for server in result.servers}) == 2
    assert {server.server_name for server in result.servers} == {"same"}


def test_subscription_same_endpoint_different_link_identity_are_two_servers() -> None:
    result = parse_subscription_payload(
        "vless://uuid-a@one.example:443?type=tcp&security=reality#one\n"
        "vless://uuid-a@one.example:443?security=reality&type=tcp#one\n"
    )

    assert result.ok is True
    assert len(result.servers) == 2
    assert len({server.server_id for server in result.servers}) == 2


def test_subscription_exact_same_link_twice_is_one_server() -> None:
    uri = "vless://uuid-a@one.example:443?type=tcp#same"
    result = parse_subscription_payload(f"{uri}\n{uri}\n")

    assert result.ok is True
    assert len(result.servers) == 1
    assert result.metadata["exact_duplicate_count"] == 1


def test_subscription_yaml_uses_stable_identity_not_name() -> None:
    result = parse_subscription_payload(
        "proxies:\n"
        "  - name: same\n"
        "    type: vless\n"
        "    server: one.example\n"
        "    port: 443\n"
        "    uuid: uuid-a\n"
        "  - name: same\n"
        "    type: vless\n"
        "    server: two.example\n"
        "    port: 443\n"
        "    uuid: uuid-b\n"
    )

    assert result.ok is True
    assert len(result.servers) == 2
    assert all(server.server_id.startswith("sub:") for server in result.servers)


def test_subscription_json_profile_parses_vless_outbounds() -> None:
    result = parse_subscription_payload(
        json.dumps(
            [
                {
                    "remarks": "profile",
                    "outbounds": [
                        {
                            "tag": "proxy",
                            "protocol": "vless",
                            "settings": {
                                "vnext": [
                                    {
                                        "address": "one.example",
                                        "port": 443,
                                        "users": [{"id": "uuid-a", "flow": "xtls-rprx-vision"}],
                                    }
                                ]
                            },
                            "streamSettings": {
                                "network": "tcp",
                                "security": "reality",
                                "realitySettings": {
                                    "serverName": "one.example",
                                    "publicKey": "public-key",
                                    "shortId": "short-id",
                                    "fingerprint": "chrome",
                                },
                            },
                        },
                        {"tag": "direct", "protocol": "freedom"},
                    ],
                }
            ]
        )
    )

    assert result.ok is True
    assert result.metadata["detected_format"] == "json_profile"
    assert len(result.servers) == 1
    assert result.servers[0].server_name == "profile"
    assert result.servers[0].host == "one.example"
    assert result.metadata["internal_endpoint_count"] == 1
    assert result.metadata["service_outbound_count"] == 1
    assert result.servers[0].raw["_fwrouter_topology"]["endpoints"][0]["host"] == "one.example"


def test_subscription_json_profile_internal_outbounds_are_not_user_servers() -> None:
    outbounds = []
    for index in range(1, 106):
        outbounds.append(
            {
                "tag": f"proxy-{index}",
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": f"node-{index}.example",
                            "port": 443,
                            "users": [{"id": f"uuid-{index}"}],
                        }
                    ]
                },
                "streamSettings": {"network": "tcp", "security": "reality"},
            }
        )
    outbounds.extend([
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "block", "protocol": "blackhole"},
    ])

    result = parse_subscription_payload(
        json.dumps(
            [
                {
                    "remarks": "logical auto profile",
                    "routing": {
                        "balancers": [
                            {"tag": "balancer", "selector": ["proxy-1", "proxy-2"]}
                        ],
                        "rules": [{"type": "field", "balancerTag": "balancer"}],
                    },
                    "outbounds": outbounds,
                }
            ]
        )
    )

    assert result.ok is True
    assert len(result.servers) == 1
    assert result.servers[0].server_name == "logical auto profile"
    assert result.metadata["internal_endpoint_count"] == 105
    assert result.metadata["service_outbound_count"] == 2
    topology = result.servers[0].raw["_fwrouter_topology"]
    assert len(topology["endpoints"]) == 105
    assert topology["balancers"][0]["tag"] == "balancer"


def test_subscription_json_two_logical_profiles_can_share_endpoint_without_physical_duplicates() -> None:
    outbound = {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": "shared.example",
                    "port": 443,
                    "users": [{"id": "uuid-shared"}],
                }
            ]
        },
        "streamSettings": {"network": "tcp", "security": "reality"},
    }

    result = parse_subscription_payload(
        json.dumps(
            [
                {"remarks": "profile a", "outbounds": [outbound, {"tag": "direct", "protocol": "freedom"}]},
                {"remarks": "profile b", "outbounds": [outbound, {"tag": "block", "protocol": "blackhole"}]},
            ]
        )
    )

    assert result.ok is True
    assert [server.server_name for server in result.servers] == ["profile a", "profile b"]
    assert result.metadata["internal_endpoint_count"] == 2
    assert all(len(server.raw["_fwrouter_topology"]["endpoints"]) == 1 for server in result.servers)


def test_subscription_membership_same_server_in_two_sources_survives_one_removal(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    uri = "vless://uuid-a@one.example:443?type=tcp#alpha"
    result = parse_subscription_payload(uri)
    empty = SubscriptionRefreshResult(
        status=SubscriptionRefreshStatus.SUCCESS,
        servers=[],
        metadata={"servers_count": 0},
    )
    adapter = _FakeSubscriptionAdapterByUrl(
        {
            "https://one.example/sub": result,
            "https://two.example/sub": result,
        }
    )
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)

    first = refresh_subscription_inventory_batch(["https://one.example/sub", "https://two.example/sub"])
    adapter.results["https://two.example/sub"] = empty
    second = refresh_subscription_inventory_batch(["https://one.example/sub", "https://two.example/sub"])

    server_id = result.servers[0].server_id
    with subscription_service.db_session() as connection:
        server = connection.execute(
            "SELECT inventory_state FROM servers WHERE server_id = ?",
            (server_id,),
        ).fetchone()
        memberships = connection.execute(
            """
            SELECT source_url, is_active
            FROM subscription_server_memberships
            WHERE server_id = ?
            ORDER BY source_url
            """,
            (server_id,),
        ).fetchall()

    assert first["ok"] is True
    assert second["ok"] is True
    assert server["inventory_state"] == "active"
    assert [(row["source_url"], row["is_active"]) for row in memberships] == [
        ("https://one.example/sub", 1),
        ("https://two.example/sub", 0),
    ]


def test_subscription_source_removal_preserves_server_level_vpn_auto_preferences(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    uri = "vless://uuid-a@one.example:443?type=tcp#alpha"
    result = parse_subscription_payload(uri)
    empty = SubscriptionRefreshResult(
        status=SubscriptionRefreshStatus.SUCCESS,
        servers=[],
        metadata={"servers_count": 0},
    )
    adapter = _FakeSubscriptionAdapterByUrl(
        {
            "https://one.example/sub": result,
            "https://two.example/sub": result,
        }
    )
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)

    refresh_subscription_inventory_batch(["https://one.example/sub", "https://two.example/sub"])
    server_id = result.servers[0].server_id
    with subscription_service.db_session() as connection:
        connection.execute(
            """
            UPDATE server_preferences
            SET vpn_auto = 1,
                vpn_auto_priority = 4,
                vpn_auto_priority_origin = 'manual',
                updated_at = CURRENT_TIMESTAMP
            WHERE server_id = ?
            """,
            (server_id,),
        )

    adapter.results["https://two.example/sub"] = empty
    refreshed = refresh_subscription_inventory_batch(["https://one.example/sub", "https://two.example/sub"])

    with subscription_service.db_session() as connection:
        row = connection.execute(
            """
            SELECT s.inventory_state, p.vpn_auto, p.vpn_auto_priority, p.vpn_auto_priority_origin
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            WHERE s.server_id = ?
            """,
            (server_id,),
        ).fetchone()

    assert refreshed["ok"] is True
    assert row["inventory_state"] == "active"
    assert row["vpn_auto"] == 1
    assert row["vpn_auto_priority"] == 4
    assert row["vpn_auto_priority_origin"] == "manual"


def test_subscription_identity_churn_carries_forward_server_preferences(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    old_result = parse_subscription_payload("vless://uuid-a@one.example:443?type=tcp#alpha")
    new_result = parse_subscription_payload("vless://uuid-b@one.example:443?type=tcp#alpha")
    adapter = _FakeSubscriptionAdapterByUrl({"https://one.example/sub": old_result})
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)

    refresh_subscription_inventory_batch(["https://one.example/sub"])
    old_id = old_result.servers[0].server_id
    with subscription_service.db_session() as connection:
        connection.execute(
            """
            UPDATE server_preferences
            SET vpn_auto = 1,
                vpn_auto_priority = 4,
                vpn_auto_priority_origin = 'manual',
                updated_at = CURRENT_TIMESTAMP
            WHERE server_id = ?
            """,
            (old_id,),
        )

    adapter.results["https://one.example/sub"] = new_result
    refreshed = refresh_subscription_inventory_batch(["https://one.example/sub"])
    new_id = new_result.servers[0].server_id

    with subscription_service.db_session() as connection:
        rows = connection.execute(
            """
            SELECT s.server_id, s.inventory_state, p.vpn_auto, p.vpn_auto_priority, p.vpn_auto_priority_origin
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            WHERE s.server_id IN (?, ?)
            ORDER BY s.server_id
            """,
            (old_id, new_id),
        ).fetchall()

    by_id = {row["server_id"]: row for row in rows}
    assert refreshed["ok"] is True
    assert refreshed["inventory"]["preference_transfer_count"] == 1
    assert by_id[old_id]["inventory_state"] == "missing"
    assert by_id[new_id]["inventory_state"] == "active"
    assert by_id[new_id]["vpn_auto"] == 1
    assert by_id[new_id]["vpn_auto_priority"] == 4
    assert by_id[new_id]["vpn_auto_priority_origin"] == "manual"


def test_subscription_remove_final_membership_marks_server_missing(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    uri = "vless://uuid-a@one.example:443?type=tcp#alpha"
    result = parse_subscription_payload(uri)
    empty = SubscriptionRefreshResult(
        status=SubscriptionRefreshStatus.SUCCESS,
        servers=[],
        metadata={"servers_count": 0},
    )
    adapter = _FakeSubscriptionAdapterByUrl({"https://one.example/sub": result})
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)

    refresh_subscription_inventory_batch(["https://one.example/sub"])
    adapter.results["https://one.example/sub"] = empty
    refresh_subscription_inventory_batch(["https://one.example/sub"])

    with subscription_service.db_session() as connection:
        row = connection.execute(
            "SELECT inventory_state FROM servers WHERE server_id = ?",
            (result.servers[0].server_id,),
        ).fetchone()

    assert row["inventory_state"] == "missing"


def test_subscription_refresh_clears_missing_active_auto_server(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    uri = "vless://uuid-a@one.example:443?type=tcp#alpha"
    result = parse_subscription_payload(uri)
    empty = SubscriptionRefreshResult(
        status=SubscriptionRefreshStatus.SUCCESS,
        servers=[],
        metadata={"servers_count": 0},
    )
    adapter = _FakeSubscriptionAdapterByUrl({"https://one.example/sub": result})
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)

    refresh_subscription_inventory_batch(["https://one.example/sub"])
    active_id = result.servers[0].server_id
    ensure_routing_global_state()
    with subscription_service.db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET server_mode = 'auto',
                active_auto_server_id = ?
            WHERE id = 1
            """,
            (active_id,),
        )

    adapter.results["https://one.example/sub"] = empty
    removed = refresh_subscription_inventory_batch(["https://one.example/sub"])

    with subscription_service.db_session() as connection:
        routing = connection.execute(
            "SELECT active_auto_server_id FROM routing_global_state WHERE id = 1"
        ).fetchone()

    assert removed["inventory"]["stale_active_auto_cleared_count"] == 1
    assert routing["active_auto_server_id"] is None


def test_subscription_duplicate_display_names_persist_in_db(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    result = parse_subscription_payload(
        "vless://uuid-a@one.example:443?type=tcp#same\n"
        "vless://uuid-b@two.example:443?type=tcp#same\n"
    )
    monkeypatch.setattr(
        subscription_adapter_module,
        "DEFAULT_SUBSCRIPTION_ADAPTER",
        _FakeSubscriptionAdapter(result),
    )

    refresh_subscription_inventory("https://one.example/sub")

    with subscription_service.db_session() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM servers WHERE server_name = 'same'"
        ).fetchone()[0]

    assert count == 2


def test_subscription_mihomo_runtime_names_are_unique() -> None:
    result = parse_subscription_payload(
        "vless://uuid-a@one.example:443?type=tcp#same\n"
        "vless://uuid-b@two.example:443?type=tcp#same\n"
    )

    runtime_names = [server.raw["name"] for server in result.servers]
    assert len(runtime_names) == len(set(runtime_names))
    assert all(name.startswith("same [") for name in runtime_names)


def test_validate_subscription_url_rejects_empty(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    validation = validate_subscription_url("")
    assert validation["valid"] is False
    assert validation["error"]["code"] == "SUBSCRIPTION_URL_EMPTY"


def test_validate_subscription_url_rejects_invalid_scheme(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    validation = validate_subscription_url("ftp://example.test/sub")
    assert validation["valid"] is False
    assert validation["error"]["code"] == "SUBSCRIPTION_URL_INVALID_SCHEME"


def test_validate_subscription_url_accepts_https(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    validation = validate_subscription_url("https://example.test/sub")
    assert validation["valid"] is True
    assert validation["normalized_url"] == "https://example.test/sub"


def test_validate_subscription_url_rejects_placeholder_host(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    validation = validate_subscription_url("https://subscription.example/profile")
    assert validation["valid"] is False
    assert validation["error"]["code"] == "SUBSCRIPTION_URL_PLACEHOLDER_HOST"


def test_save_subscription_url_sets_idle_state(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    result = save_subscription_url("https://example.test/sub", metadata={"name": "test"})
    state = get_subscription_state()

    assert result["saved"] is True
    assert state["status"] == "idle"
    assert state["url"] == "https://example.test/sub"
    assert state["metadata"]["name"] == "test"
    sources = state["metadata"]["subscriptions"]["items"]
    assert [source["url"] for source in sources] == ["https://example.test/sub"]
    assert sources[0]["enabled"] is True
    assert sources[0]["status"] == "idle"
    assert sources[0]["servers_count"] == 0


def test_save_subscription_url_preserves_existing_backend_registry(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    first = save_subscription_url("https://one.example/sub")
    second = save_subscription_url("https://two.example/sub")
    repeat = save_subscription_url("https://one.example/sub")
    state = get_subscription_state()

    assert first["saved"] is True
    assert second["saved"] is True
    assert repeat["saved"] is True
    sources = state["metadata"]["subscriptions"]["items"]
    assert [source["url"] for source in sources] == [
        "https://one.example/sub",
        "https://two.example/sub",
    ]
    assert len(sources) == 2


def test_save_subscription_url_invalid_keeps_not_configured(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    result = save_subscription_url("bad-url")
    state = get_subscription_state()

    assert result["saved"] is False
    assert state["status"] == "not_configured"


def test_subscription_account_delete_cascades_clients(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with subscription_service.db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscription_accounts (account_id, slug, display_name, enabled)
            VALUES (100, 'cascade', 'Cascade', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO subscription_clients (client_id, account_id, token, app_type, enabled, display_name)
            VALUES (101, 100, 'cascade-token', 'auto', 1, 'Cascade token')
            """
        )
        connection.execute("DELETE FROM subscription_accounts WHERE account_id = 100")

    with subscription_service.db_session() as connection:
        row = connection.execute(
            "SELECT client_id FROM subscription_clients WHERE client_id = 101"
        ).fetchone()
        fk_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert row is None
    assert fk_errors == []


def test_legacy_orphan_subscription_client_is_ignored_by_profiles(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    raw = sqlite3.connect(get_db_path())
    try:
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute(
            """
            INSERT INTO subscription_clients (
                client_id,
                account_id,
                token,
                app_type,
                enabled,
                display_name
            )
            VALUES (5, 5, 'sveta', 'auto', 1, 'Sveta')
            """
        )
        raw.commit()
    finally:
        raw.close()

    resolved = resolve_subscription_client("sveta", None, "auto", auto_create_legacy=False)
    desired_clients = list_desired_subscription_xray_clients("sveta")

    assert resolved["ok"] is False
    assert resolved["error_code"] == "SUBSCRIPTION_CLIENT_NOT_FOUND"
    assert desired_clients == []


def test_refresh_subscription_inventory_rejects_invalid_saved_url(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    result = refresh_subscription_inventory("ftp://bad")
    state = get_subscription_state()

    assert result["ok"] is False
    assert result["stage"] == "validate"
    assert state["status"] == "failed"
    assert state["error_code"] == "SUBSCRIPTION_URL_INVALID_SCHEME"


def test_refresh_subscription_inventory_reports_saved_placeholder_url(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    with subscription_service.db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscription_state (id, url, status)
            VALUES (1, 'https://subscription.example/profile', 'idle')
            """
        )

    result = refresh_subscription_inventory()
    assert result["ok"] is False
    assert result["diagnostics"]["saved_url_invalid"] is True
    assert result["error"]["code"] == "SUBSCRIPTION_URL_PLACEHOLDER_HOST"


def test_refresh_subscription_inventory_records_adapter_failure(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    adapter = _FakeSubscriptionAdapter(_failed_refresh_result())
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)

    result = refresh_subscription_inventory()
    state = get_subscription_state()

    assert result["ok"] is False
    assert result["stage"] == "download_parse"
    assert adapter.calls == ["https://example.test/sub"]
    assert state["status"] == "failed"
    assert state["error_code"] == "SUBSCRIPTION_DOWNLOAD_FAILED"


def test_refresh_subscription_inventory_syncs_servers(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    adapter = _FakeSubscriptionAdapter(_success_refresh_result("alpha", "beta"))
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)

    result = refresh_subscription_inventory()
    state = get_subscription_state()

    assert result["ok"] is True
    assert result["inventory"]["active_count"] == 2
    assert state["status"] == "success"
    assert state["last_success_at"] is not None


def test_refresh_subscription_inventory_marks_missing_servers(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", _FakeSubscriptionAdapter(_success_refresh_result("alpha", "beta")))
    refresh_subscription_inventory()

    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", _FakeSubscriptionAdapter(_success_refresh_result("beta")))
    result = refresh_subscription_inventory()

    assert result["inventory"]["active_count"] == 1
    assert result["inventory"]["missing_count"] == 1


def test_normalize_subscription_urls_trims_ignores_empty_and_dedupes(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    result = normalize_subscription_urls([
        "  https://one.example/sub  ",
        "",
        "https://one.example/sub",
        " https://two.example/sub ",
    ])

    assert result["urls"] == ["https://one.example/sub", "https://two.example/sub"]
    assert result["empty_count"] == 1
    assert result["duplicate_count"] == 1


def test_refresh_subscription_inventory_batch_syncs_union_once(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    adapter = _FakeSubscriptionAdapterByUrl({
        "https://one.example/sub": _success_refresh_result("alpha", "beta"),
        "https://two.example/sub": _success_refresh_result("beta", "gamma"),
    })
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)

    result = refresh_subscription_inventory_batch([
        " https://one.example/sub ",
        "https://two.example/sub",
        "https://one.example/sub",
    ])

    assert result["ok"] is True
    assert adapter.calls == ["https://one.example/sub", "https://two.example/sub"]
    assert result["batch"]["added_subscriptions"] == 2
    assert result["batch"]["imported_servers"] == 3
    assert result["batch"]["duplicate_urls"] == 1
    assert result["batch"]["already_existing"] == 2
    assert result["inventory"]["seen_count"] == 3
    with subscription_service.db_session() as connection:
        states = {
            row["server_id"]: row["inventory_state"]
            for row in connection.execute("SELECT server_id, inventory_state FROM servers")
        }
    assert states == {"alpha": "active", "beta": "active", "gamma": "active"}


def test_refresh_subscription_inventory_batch_persists_authoritative_sources(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    adapter = _FakeSubscriptionAdapterByUrl({
        "https://one.example/sub": _success_refresh_result("alpha"),
        "https://two.example/sub": _success_refresh_result("beta"),
    })
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)

    result = refresh_subscription_inventory_batch([
        "https://one.example/sub",
        "https://two.example/sub",
    ])

    assert result["ok"] is True
    state = get_subscription_state()
    sources = state["metadata"]["subscriptions"]["items"]
    assert [source["url"] for source in sources] == [
        "https://one.example/sub",
        "https://two.example/sub",
    ]
    assert [source["servers_count"] for source in sources] == [1, 1]


def test_authoritative_refresh_deactivates_legacy_memberships(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    result = parse_subscription_payload("vless://uuid-a@one.example:443?type=tcp#alpha")
    server_id = result.servers[0].server_id
    with subscription_service.db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (server_id, server_name, provider_name, inventory_state)
            VALUES (?, 'alpha', 'subscription', 'active')
            """,
            (server_id,),
        )
        connection.execute(
            """
            INSERT INTO subscription_server_memberships (
                source_id, server_id, source_url, entry_identity_hash, parser_format,
                display_name, is_active
            )
            VALUES ('legacy:unknown', ?, 'legacy:unknown', 'legacy', 'legacy', 'alpha', 1)
            """,
            (server_id,),
        )
    monkeypatch.setattr(
        subscription_adapter_module,
        "DEFAULT_SUBSCRIPTION_ADAPTER",
        _FakeSubscriptionAdapterByUrl({"https://one.example/sub": result}),
    )

    refreshed = refresh_subscription_inventory_batch(["https://one.example/sub"])

    with subscription_service.db_session() as connection:
        memberships = connection.execute(
            """
            SELECT source_url, is_active
            FROM subscription_server_memberships
            WHERE server_id = ?
            ORDER BY source_url
            """,
            (server_id,),
        ).fetchall()

    assert refreshed["inventory"]["legacy_membership_deactivated_count"] == 1
    assert [(row["source_url"], row["is_active"]) for row in memberships] == [
        ("https://one.example/sub", 1),
        ("legacy:unknown", 0),
    ]


def test_subscription_batch_preserves_existing_sources_and_dedupes(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    adapter = _FakeSubscriptionAdapterByUrl({
        "https://one.example/sub": _success_refresh_result("alpha"),
        "https://two.example/sub": _success_refresh_result("beta"),
    })
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)

    refresh_subscription_inventory_batch(["https://one.example/sub"])
    adapter.calls.clear()
    result = refresh_subscription_inventory_batch([
        "https://two.example/sub",
        "https://one.example/sub",
        "https://two.example/sub",
    ])

    assert result["ok"] is True
    assert adapter.calls == ["https://one.example/sub", "https://two.example/sub"]
    assert result["batch"]["duplicate_urls"] == 1
    state = get_subscription_state()
    sources = state["metadata"]["subscriptions"]["items"]
    assert [source["url"] for source in sources] == [
        "https://one.example/sub",
        "https://two.example/sub",
    ]
    assert len(sources) == 2


def test_refresh_subscription_inventory_uses_all_persistent_sources(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    adapter = _FakeSubscriptionAdapterByUrl({
        "https://one.example/sub": _success_refresh_result("alpha"),
        "https://two.example/sub": _success_refresh_result("beta"),
    })
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)
    refresh_subscription_inventory_batch([
        "https://one.example/sub",
        "https://two.example/sub",
    ])

    adapter.calls.clear()
    result = refresh_subscription_inventory()

    assert result["ok"] is True
    assert adapter.calls == ["https://one.example/sub", "https://two.example/sub"]
    with subscription_service.db_session() as connection:
        states = {
            row["server_id"]: row["inventory_state"]
            for row in connection.execute("SELECT server_id, inventory_state FROM servers")
        }
    assert states == {"alpha": "active", "beta": "active"}


def test_refresh_subscription_inventory_imports_legacy_url_into_backend_registry(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with subscription_service.db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscription_state (
                id,
                url,
                status,
                metadata_json
            )
            VALUES (1, ?, 'success', ?)
            """,
            (
                "https://legacy.example/sub",
                json.dumps({"batch": {"submitted_count": 1}}, sort_keys=True),
            ),
        )
    adapter = _FakeSubscriptionAdapterByUrl({
        "https://legacy.example/sub": _success_refresh_result("legacy-alpha"),
    })
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)

    plan_before = subscription_registry_import_plan()
    result = refresh_subscription_inventory()
    plan_after = subscription_registry_import_plan()

    assert plan_before["needed"] is True
    assert result["ok"] is True
    assert adapter.calls == ["https://legacy.example/sub"]
    assert plan_after["needed"] is False
    state = get_subscription_state()
    sources = state["metadata"]["subscriptions"]["items"]
    assert [source["url"] for source in sources] == ["https://legacy.example/sub"]
    assert sources[0]["status"] == "success"
    assert sources[0]["servers_count"] == 1


def test_refresh_subscription_inventory_empty_registry_falls_back_to_legacy_url(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    with subscription_service.db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscription_state (
                id,
                url,
                status,
                metadata_json
            )
            VALUES (1, ?, 'idle', ?)
            """,
            (
                "https://legacy.example/sub",
                json.dumps({"subscriptions": {"version": 1, "items": []}}, sort_keys=True),
            ),
        )
    adapter = _FakeSubscriptionAdapterByUrl({
        "https://legacy.example/sub": _success_refresh_result("legacy-alpha"),
    })
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)

    result = refresh_subscription_inventory()

    assert result["ok"] is True
    assert adapter.calls == ["https://legacy.example/sub"]
    state = get_subscription_state()
    assert [source["url"] for source in state["metadata"]["subscriptions"]["items"]] == [
        "https://legacy.example/sub"
    ]


def test_refresh_subscription_inventory_preserves_other_source_servers(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    adapter = _FakeSubscriptionAdapterByUrl({
        "https://one.example/sub": _success_refresh_result("alpha"),
        "https://two.example/sub": _success_refresh_result("beta"),
    })
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)
    refresh_subscription_inventory_batch([
        "https://one.example/sub",
        "https://two.example/sub",
    ])

    adapter.results = {
        "https://one.example/sub": _success_refresh_result("alpha"),
        "https://two.example/sub": _failed_refresh_result(),
    }
    result = refresh_subscription_inventory()

    assert result["ok"] is True
    with subscription_service.db_session() as connection:
        states = {
            row["server_id"]: row["inventory_state"]
            for row in connection.execute("SELECT server_id, inventory_state FROM servers")
        }
    assert states == {"alpha": "active", "beta": "active"}
    state = get_subscription_state()
    sources = {source["url"]: source for source in state["metadata"]["subscriptions"]["items"]}
    assert sources["https://two.example/sub"]["used_last_good"] is True


def test_refresh_subscription_inventory_all_failures_keep_last_good_inventory(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    adapter = _FakeSubscriptionAdapterByUrl({
        "https://one.example/sub": _success_refresh_result("alpha"),
        "https://two.example/sub": _success_refresh_result("beta"),
    })
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)
    refresh_subscription_inventory_batch([
        "https://one.example/sub",
        "https://two.example/sub",
    ])

    adapter.results = {
        "https://one.example/sub": _failed_refresh_result(),
        "https://two.example/sub": _failed_refresh_result(),
    }
    result = refresh_subscription_inventory()

    assert result["ok"] is False
    with subscription_service.db_session() as connection:
        states = {
            row["server_id"]: row["inventory_state"]
            for row in connection.execute("SELECT server_id, inventory_state FROM servers")
        }
    assert states == {"alpha": "active", "beta": "active"}


def test_refresh_subscription_inventory_shared_server_survives_source_removal(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    adapter = _FakeSubscriptionAdapterByUrl({
        "https://one.example/sub": _success_refresh_result("alpha", "shared"),
        "https://two.example/sub": _success_refresh_result("shared", "beta"),
    })
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)
    refresh_subscription_inventory_batch([
        "https://one.example/sub",
        "https://two.example/sub",
    ])

    adapter.results = {
        "https://one.example/sub": _success_refresh_result(),
        "https://two.example/sub": _success_refresh_result("shared", "beta"),
    }
    result = refresh_subscription_inventory()

    assert result["ok"] is True
    with subscription_service.db_session() as connection:
        states = {
            row["server_id"]: row["inventory_state"]
            for row in connection.execute("SELECT server_id, inventory_state FROM servers")
        }
    assert states == {"alpha": "missing", "shared": "active", "beta": "active"}


def test_subscription_batch_endpoint_continues_after_one_url_error(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    adapter = _FakeSubscriptionAdapterByUrl({
        "https://ok.example/sub": _success_refresh_result("alpha"),
    })
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)
    apply_calls: list[dict[str, object]] = []

    def _fake_apply_import(refresh_result):
        apply_calls.append(refresh_result)
        return {
            "ok": bool(refresh_result.get("ok")),
            "stage": "already_current",
            "refresh": refresh_result,
            "candidate": {"candidate_path": str(tmp_path / "candidate.yaml")},
            "config_validation": {"ok": True},
            "promoted": False,
            "container_restarted": False,
            "applied": False,
            "auto_select": {"ok": True, "triggered": False},
            "error": None,
        }

    monkeypatch.setattr(subscription_route, "apply_subscription_import_result", _fake_apply_import)

    response = _client().post(
        "/api/v2/subscription",
        json={
            "urls": [
                "https://ok.example/sub",
                "ftp://bad",
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["batch"]["added_subscriptions"] == 1
    assert body["data"]["batch"]["errors"] == 1
    assert body["data"]["batch"]["items"][0]["url_saved"] is True
    assert "url" not in body["data"]["batch"]["items"][0]
    assert body["data"]["applied"] is False
    assert body["data"]["container_restarted"] is False
    assert len(apply_calls) == 1


def test_apply_subscription_import_result_reuses_existing_batch_import(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    calls: list[str] = []
    monkeypatch.setattr(
        pipeline_service,
        "write_mihomo_candidate_config",
        lambda: calls.append("candidate") or {"candidate_path": str(tmp_path / "candidate.yaml")},
    )
    monkeypatch.setattr(
        pipeline_service,
        "validate_mihomo_candidate_config",
        lambda: calls.append("validate") or {"ok": True, "returncode": 0, "stdout_tail": "", "stderr_tail": ""},
    )
    monkeypatch.setattr(
        pipeline_service,
        "reconcile_mihomo_runtime",
        lambda: calls.append("reconcile") or {
            "ok": True,
            "reconcile_action": "none",
            "reconcile_reason": "unchanged_config",
            "promoted": {"ok": True, "promoted": False},
            "container": {"ok": True, "action": "none"},
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "get_routing_global_state",
        lambda: {"server_mode": "fixed"},
    )
    monkeypatch.setattr(
        pipeline_service,
        "get_vpn_auto_state",
        lambda: {"server_mode": "fixed"},
    )

    result = pipeline_service.apply_subscription_import_result({
        "ok": True,
        "stage": "inventory_synced",
        "state": get_subscription_state(),
        "batch": {"items": [], "added_subscriptions": 1, "errors": 0},
        "inventory": {"seen_count": 1},
    })

    assert result["ok"] is True
    assert result["stage"] == "already_current"
    assert calls == ["candidate", "validate", "reconcile"]


def test_prepare_subscription_refresh_stops_on_refresh_failure(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", _FakeSubscriptionAdapter(_failed_refresh_result()))

    result = pipeline_service.prepare_subscription_refresh()

    assert result["ok"] is False
    assert result["stage"] == "download_parse"
    assert result["candidate"] is None
    assert result["promoted"] is False
    assert result["container_restarted"] is False


def test_prepare_subscription_refresh_stops_on_config_validation_failure(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", _FakeSubscriptionAdapter(_success_refresh_result("alpha")))
    monkeypatch.setattr(
        pipeline_service,
        "write_mihomo_candidate_config",
        lambda: {"candidate_path": str(tmp_path / "candidate.yaml")},
    )
    monkeypatch.setattr(
        pipeline_service,
        "validate_mihomo_candidate_config",
        lambda: {"ok": False, "returncode": 1, "stdout_tail": "", "stderr_tail": "bad"},
    )

    result = pipeline_service.prepare_subscription_refresh()

    assert result["ok"] is False
    assert result["stage"] == "config_validation"
    assert result["promoted"] is False
    assert result["container_restarted"] is False


def test_prepare_subscription_refresh_success_keeps_candidate_only(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", _FakeSubscriptionAdapter(_success_refresh_result("alpha")))
    monkeypatch.setattr(
        pipeline_service,
        "write_mihomo_candidate_config",
        lambda: {"candidate_path": str(tmp_path / "candidate.yaml"), "active_path": str(tmp_path / "active.yaml")},
    )
    monkeypatch.setattr(
        pipeline_service,
        "validate_mihomo_candidate_config",
        lambda: {"ok": True, "returncode": 0, "stdout_tail": "ok", "stderr_tail": ""},
    )

    result = pipeline_service.prepare_subscription_refresh()

    assert result["ok"] is True
    assert result["stage"] == "candidate_validated"
    assert result["promoted"] is False
    assert result["container_restarted"] is False


def test_apply_subscription_refresh_skips_runtime_when_config_is_unchanged(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        pipeline_service,
        "prepare_subscription_refresh",
        lambda: {
            "ok": True,
            "stage": "candidate_validated",
            "refresh": {"ok": True},
            "candidate": {"candidate_path": str(tmp_path / "candidate.yaml")},
            "config_validation": {"ok": True},
            "promoted": False,
            "container_restarted": False,
            "error": None,
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "reconcile_mihomo_runtime",
        lambda: {
            "ok": True,
            "reconcile_action": "none",
            "reconcile_reason": "unchanged_config",
            "promoted": {"ok": True, "promoted": False, "reason": "unchanged_config"},
            "container": {"ok": True, "action": "none", "reason": "unchanged_config"},
        },
    )

    result = pipeline_service.apply_subscription_refresh()

    assert result["ok"] is True
    assert result["stage"] == "already_current"
    assert result["applied"] is False
    assert result["promoted"] is False
    assert result["container_restarted"] is False


def test_apply_subscription_refresh_promotes_and_restarts_when_config_changed(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        pipeline_service,
        "prepare_subscription_refresh",
        lambda: {
            "ok": True,
            "stage": "candidate_validated",
            "refresh": {"ok": True},
            "candidate": {"candidate_path": str(tmp_path / "candidate.yaml")},
            "config_validation": {"ok": True},
            "promoted": False,
            "container_restarted": False,
            "error": None,
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "reconcile_mihomo_runtime",
        lambda: {
            "ok": True,
            "reconcile_action": "restart",
            "reconcile_reason": "config_reload_required",
            "promoted": {"ok": True, "promoted": True},
            "container": {"ok": True, "action": "restart"},
        },
    )

    result = pipeline_service.apply_subscription_refresh()

    assert result["ok"] is True
    assert result["stage"] == "applied"
    assert result["applied"] is True
    assert result["promoted"] is True
    assert result["container_restarted"] is True


def test_apply_subscription_refresh_reports_runtime_reconcile_failure(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        pipeline_service,
        "prepare_subscription_refresh",
        lambda: {
            "ok": True,
            "stage": "candidate_validated",
            "refresh": {"ok": True},
            "candidate": {"candidate_path": str(tmp_path / "candidate.yaml")},
            "config_validation": {"ok": True},
            "promoted": False,
            "container_restarted": False,
            "error": None,
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "reconcile_mihomo_runtime",
        lambda: {
            "ok": False,
            "reconcile_action": "restart",
            "reconcile_reason": "config_reload_required",
            "promoted": {"ok": True, "promoted": True},
            "container": {"ok": False, "action": "restart", "error_code": "MIHOMO_RESTART_FAILED", "error_message": "restart failed"},
        },
    )

    result = pipeline_service.apply_subscription_refresh()

    assert result["ok"] is False
    assert result["stage"] == "apply_runtime"
    assert result["error"]["code"] == "MIHOMO_RESTART_FAILED"


def test_subscription_refresh_selects_active_auto_when_empty(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        pipeline_service,
        "prepare_subscription_refresh",
        lambda: {
            "ok": True,
            "stage": "candidate_validated",
            "refresh": {"ok": True},
            "candidate": {"candidate_path": str(tmp_path / "candidate.yaml")},
            "config_validation": {"ok": True},
            "promoted": False,
            "container_restarted": False,
            "error": None,
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "reconcile_mihomo_runtime",
        lambda: {
            "ok": True,
            "reconcile_action": "none",
            "reconcile_reason": "unchanged_config",
            "promoted": {"ok": True, "promoted": False},
            "container": {"ok": True, "action": "none"},
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "get_routing_global_state",
        lambda: {"server_mode": "auto"},
    )
    state_calls = {"count": 0}

    def _fake_state():
        state_calls["count"] += 1
        if state_calls["count"] == 1:
            return {
                "server_mode": "auto",
                "enabled_candidates_count": 2,
                "auto_selectable_candidates_count": 2,
                "active_auto_server_id": None,
                "active_auto_server_valid": False,
            }
        return {
            "server_mode": "auto",
            "enabled_candidates_count": 2,
            "auto_selectable_candidates_count": 2,
            "active_auto_server_id": "alpha",
            "active_auto_server_valid": True,
        }

    selector_calls: list[dict[str, object]] = []
    monkeypatch.setattr(pipeline_service, "get_vpn_auto_state", _fake_state)
    monkeypatch.setattr(
        pipeline_service,
        "select_vpn_auto_server",
        lambda **kwargs: selector_calls.append(kwargs) or {"ok": True, "selected_server_id": "alpha", "active_after": "alpha"},
    )

    result = pipeline_service.apply_subscription_refresh()

    assert result["ok"] is True
    assert result["auto_select"]["triggered"] is True
    assert selector_calls


def test_subscription_refresh_reselects_when_active_server_removed(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        pipeline_service,
        "prepare_subscription_refresh",
        lambda: {
            "ok": True,
            "stage": "candidate_validated",
            "refresh": {"ok": True},
            "candidate": {"candidate_path": str(tmp_path / "candidate.yaml")},
            "config_validation": {"ok": True},
            "promoted": False,
            "container_restarted": False,
            "error": None,
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "reconcile_mihomo_runtime",
        lambda: {
            "ok": True,
            "reconcile_action": "restart",
            "reconcile_reason": "config_reload_required",
            "promoted": {"ok": True, "promoted": True},
            "container": {"ok": True, "action": "restart"},
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "get_routing_global_state",
        lambda: {"server_mode": "auto"},
    )
    state_calls = {"count": 0}

    def _fake_state():
        state_calls["count"] += 1
        if state_calls["count"] == 1:
            return {
                "server_mode": "auto",
                "enabled_candidates_count": 1,
                "auto_selectable_candidates_count": 1,
                "active_auto_server_id": "removed",
                "active_auto_server_valid": False,
            }
        return {
            "server_mode": "auto",
            "enabled_candidates_count": 1,
            "auto_selectable_candidates_count": 1,
            "active_auto_server_id": "beta",
            "active_auto_server_valid": True,
        }

    selector_calls: list[dict[str, object]] = []
    monkeypatch.setattr(pipeline_service, "get_vpn_auto_state", _fake_state)
    monkeypatch.setattr(
        pipeline_service,
        "select_vpn_auto_server",
        lambda **kwargs: selector_calls.append(kwargs) or {"ok": True, "selected_server_id": "beta", "active_after": "beta"},
    )

    result = pipeline_service.apply_subscription_refresh()

    assert result["ok"] is True
    assert result["auto_select"]["status"] == "auto_selected"
    assert selector_calls


def test_subscription_refresh_does_not_select_when_server_mode_fixed(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr(
        pipeline_service,
        "prepare_subscription_refresh",
        lambda: {
            "ok": True,
            "stage": "candidate_validated",
            "refresh": {"ok": True},
            "candidate": {"candidate_path": str(tmp_path / "candidate.yaml")},
            "config_validation": {"ok": True},
            "promoted": False,
            "container_restarted": False,
            "error": None,
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "reconcile_mihomo_runtime",
        lambda: {
            "ok": True,
            "reconcile_action": "none",
            "reconcile_reason": "unchanged_config",
            "promoted": {"ok": True, "promoted": False},
            "container": {"ok": True, "action": "none"},
        },
    )
    monkeypatch.setattr(
        pipeline_service,
        "get_routing_global_state",
        lambda: {"server_mode": "fixed"},
    )
    monkeypatch.setattr(
        pipeline_service,
        "get_vpn_auto_state",
        lambda: {"server_mode": "fixed"},
    )

    called = {"value": False}
    monkeypatch.setattr(
        pipeline_service,
        "select_vpn_auto_server",
        lambda **kwargs: called.__setitem__("value", True) or {"ok": True},
    )

    result = pipeline_service.apply_subscription_refresh()

    assert result["ok"] is True
    assert result["auto_select"]["triggered"] is False
    assert called["value"] is False


def test_subscription_get_endpoint_redacts_url(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")

    response = _client().get("/api/v2/subscription")

    assert response.status_code == 200
    payload = response.json()["data"]["subscription"]
    assert payload["url_saved"] is True
    assert "url" not in payload


def test_subscription_validate_endpoint_returns_error(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    response = _client().post("/api/v2/subscription/validate", json={"url": "ftp://bad"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "SUBSCRIPTION_URL_INVALID_SCHEME"


def test_subscription_save_endpoint_redacts_url(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    response = _client().post("/api/v2/subscription", json={"url": "https://example.test/sub", "metadata": {"source": "pytest"}})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["subscription"]["url_saved"] is True
    assert "url" not in body["data"]["subscription"]


def test_subscription_refresh_endpoint_returns_accepted_job_without_inline_work(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    manager = _FakeRefreshJobManager()
    monkeypatch.setattr(subscription_route, "get_default_job_manager", lambda: manager)
    monkeypatch.setattr(
        subscription_refresh_job,
        "apply_subscription_refresh",
        lambda: (_ for _ in ()).throw(AssertionError("refresh must not run inline in HTTP request")),
    )

    response = _client().post("/api/v2/subscription/refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["accepted"] is True
    assert body["data"]["job_id"] == "job-refresh-1"
    assert body["data"]["job"]["job_type"] == "subscription_refresh"
    assert manager.created[0]["lock_key"] == "subscription_refresh"
    assert manager.started == ["job-refresh-1"]


def test_subscription_refresh_endpoint_returns_existing_job_when_already_running(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    existing = {
        "job_id": "job-refresh-existing",
        "job_type": "subscription_refresh",
        "status": "running",
        "lock_key": "subscription_refresh",
    }

    class _ConflictManager(_FakeRefreshJobManager):
        def create(self, job_type: str, **kwargs: object) -> dict[str, object]:
            raise JobLockConflictError("subscription_refresh", existing)

    monkeypatch.setattr(subscription_route, "get_default_job_manager", lambda: _ConflictManager())
    response = _client().post("/api/v2/subscription/refresh")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["accepted"] is False
    assert body["data"]["already_running"] is True
    assert body["data"]["job_id"] == "job-refresh-existing"


def test_generic_jobs_api_rejects_persistence_only_subscription_prepare(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    response = _client().post(
        "/api/v2/jobs",
        json={"job_type": "subscription_refresh_prepare", "run_now": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "JOB_TYPE_NOT_ALLOWED"


def test_subscription_refresh_job_success_finishes_after_verify(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    subscription_refresh_job.register_subscription_refresh_handler(manager)
    monkeypatch.setattr(
        subscription_refresh_job,
        "apply_subscription_refresh",
        lambda: {
            "ok": True,
            "stage": "applied",
            "refresh": {
                "validation": {"valid": True, "normalized_url": "https://example.test/sub", "error": None},
                "state": get_subscription_state(),
                "refresh": _success_refresh_result("alpha").to_dict(),
            },
            "candidate": {"candidate_path": str(tmp_path / "candidate.yaml")},
            "config_validation": {"ok": True},
            "promoted": True,
            "container_restarted": True,
            "applied": True,
            "reconcile_action": "force_recreate",
            "reconcile_reason": "structural_change",
        },
    )

    job = manager.create("subscription_refresh", lock_key="subscription_refresh", requested_by="pytest")
    final = manager.run_job(job["job_id"])

    assert final is not None
    assert final["status"] == "success"
    assert final["result"]["stage"] == "verify"
    assert final["result"]["runtime_verified"] is True
    assert final["result"]["operation"] == "subscription_refresh"


def test_subscription_refresh_job_download_failure_reports_stage(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    subscription_refresh_job.register_subscription_refresh_handler(manager)
    monkeypatch.setattr(
        subscription_refresh_job,
        "apply_subscription_refresh",
        lambda: {
            "ok": False,
            "stage": "download_parse",
            "refresh": {"validation": {"valid": True, "normalized_url": "https://example.test/sub"}},
            "error": {"code": "SUBSCRIPTION_DOWNLOAD_FAILED", "message": "download failed"},
        },
    )

    job = manager.create("subscription_refresh", lock_key="subscription_refresh", requested_by="pytest")
    final = manager.run_job(job["job_id"])

    assert final is not None
    assert final["status"] == "failed"
    assert final["error_code"] == "SUBSCRIPTION_DOWNLOAD_FAILED"
    assert final["result"]["stage"] == "download"
    assert final["result"]["job_id"] == job["job_id"]
    assert final["result"]["operation"] == "subscription_refresh"


def test_subscription_refresh_job_parse_failure_reports_stage(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    subscription_refresh_job.register_subscription_refresh_handler(manager)
    monkeypatch.setattr(
        subscription_refresh_job,
        "apply_subscription_refresh",
        lambda: {
            "ok": False,
            "stage": "download_parse",
            "refresh": {"validation": {"valid": True, "normalized_url": "https://example.test/sub"}},
            "error": {"code": "SUBSCRIPTION_PARSE_FAILED", "message": "parse failed"},
        },
    )

    job = manager.create("subscription_refresh", lock_key="subscription_refresh", requested_by="pytest")
    final = manager.run_job(job["job_id"])

    assert final is not None
    assert final["status"] == "failed"
    assert final["error_code"] == "SUBSCRIPTION_PARSE_FAILED"
    assert final["result"]["stage"] == "parse"


def test_subscription_refresh_job_apply_and_verify_failures_do_not_succeed(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    manager = JobManager()
    subscription_refresh_job.register_subscription_refresh_handler(manager)

    failures = [
        ("apply_runtime", "SUBSCRIPTION_RUNTIME_RECONCILE_FAILED"),
        ("applied", "VPN_AUTO_AUTOSELECT_FAILED"),
    ]
    for stage, code in failures:
        monkeypatch.setattr(
            subscription_refresh_job,
            "apply_subscription_refresh",
            lambda stage=stage, code=code: {
                "ok": False,
                "stage": stage,
                "refresh": {"validation": {"valid": True, "normalized_url": "https://example.test/sub"}},
                "error": {"code": code, "message": f"{stage} failed"},
            },
        )
        job = manager.create("subscription_refresh", lock_key="subscription_refresh", requested_by="pytest")
        final = manager.run_job(job["job_id"])

        assert final is not None
        assert final["status"] == "failed"
        assert final["error_code"] == code
        assert final["result"]["stage"] == ("verify" if stage == "applied" else "apply_runtime")


def test_subscription_refresh_stale_job_releases_lock(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_JOB_STALE_TIMEOUT_SECONDS", "5")
    get_settings.cache_clear()
    initialize_database()
    manager = JobManager()

    stale = manager.create("subscription_refresh", lock_key="subscription_refresh", requested_by="pytest")
    mark_job_running(stale["job_id"])
    with subscription_service.db_session() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET started_at = datetime('now', '-5 minutes'),
                updated_at = datetime('now', '-5 minutes')
            WHERE job_id = ?
            """,
            (stale["job_id"],),
        )

    fresh = manager.create("subscription_refresh", lock_key="subscription_refresh", requested_by="pytest")
    stale_after = get_job(stale["job_id"])

    assert stale_after is not None
    assert stale_after["status"] == "failed"
    assert stale_after["error_code"] == "JOB_STALE_TIMEOUT"
    assert fresh["job_id"] != stale["job_id"]


def test_subscription_refresh_success_does_not_mutate_subject_inventory(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    save_subscription_url("https://example.test/sub")
    monkeypatch.setattr(subscription_adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", _FakeSubscriptionAdapter(_success_refresh_result("alpha")))

    refresh_subscription_inventory()
    subjects = list_subjects(limit=100)

    assert subjects == []
