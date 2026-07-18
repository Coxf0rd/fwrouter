from __future__ import annotations

from pathlib import Path

import pytest

from fwrouter_api.adapters.rules_sources import (
    RulesSourceAdapter,
    RulesSourceFetchError,
    RulesSourcePayload,
)
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services.jobs import create_job
from fwrouter_api.services.rules import (
    validate_value_list,
    validate_manual_rules,
    get_manual_rules_texts,
    get_rules_overview,
    run_rules_full_update,
)
from fwrouter_api.services.routing_manifest import build_dataplane_manifest_from_state


GOOD_BIG_VPN_URL = (
    "git+https://github.com/1andrevich/Re-filter-lists.git"
    "?ref=main&path=community.lst&path=community_ips.lst"
)
AGGREGATE_BIG_VPN_URL = (
    "git+https://github.com/1andrevich/Re-filter-lists.git"
    "?ref=main&path=domains_all.lst&path=ipsum.lst"
)


def _configure_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    get_settings.cache_clear()


def _patch_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    import fwrouter_api.services.dnsmasq as dnsmasq_service

    monkeypatch.setattr(
        "fwrouter_api.services.rules.run_apply_pipeline",
        lambda **kwargs: {
            "ok": True,
            "dataplane_capability": "nft_owned_table",
            "enforcement_level": "owned_table_ready",
            "traffic_enforcement_guaranteed": False,
            "supported_modes": {"direct": True, "vpn": True, "selective": True},
            "missing_runtime_requirements": [],
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.rules.reconcile_mihomo_runtime",
        lambda: {"ok": True, "stage": "reconcile"},
    )
    monkeypatch.setattr(
        dnsmasq_service,
        "reconcile_dnsmasq_rules",
        lambda: {"ok": True, "stage": "reconcile"},
    )


class _FakeRulesSourceAdapter:
    def __init__(self, *, big_vpn_payload: RulesSourcePayload) -> None:
        self._big_vpn_payload = big_vpn_payload

    def fetch_big_direct_sources(self) -> RulesSourcePayload:
        return RulesSourcePayload(
            values=[],
            source_urls=[],
            version_name="big_direct:empty",
            fetch_metadata=[],
        )

    def fetch_big_vpn_sources(self) -> RulesSourcePayload:
        return self._big_vpn_payload


class _FailingBigVpnRulesSourceAdapter:
    def fetch_big_direct_sources(self) -> RulesSourcePayload:
        return RulesSourcePayload(
            values=[],
            source_urls=[],
            version_name="big_direct:empty",
            fetch_metadata=[],
        )

    def fetch_big_vpn_sources(self) -> RulesSourcePayload:
        raise RulesSourceFetchError(
            code="RULES_SOURCE_TIMEOUT",
            message="Rules source timed out for big_vpn: https://example.invalid/ipsum.lst",
            details={"channel": "big_vpn", "timeout_seconds": 30},
        )


def _create_job() -> dict[str, object]:
    return create_job(
        "rules_full_update",
        requested_by="pytest",
        input_data={"requested_by": "pytest"},
    )


def _good_big_vpn_payload() -> RulesSourcePayload:
    return RulesSourcePayload(
        values=["openai.com", "1.1.1.1/32"],
        source_urls=[GOOD_BIG_VPN_URL],
        version_name="big_vpn:good",
        fetch_metadata=[
            {
                "configured_url": GOOD_BIG_VPN_URL,
                "url": "https://github.com/1andrevich/Re-filter-lists.git#community.lst",
                "source_kind": "git_repo",
                "path": "community.lst",
                "raw_text": "openai.com\n",
            },
            {
                "configured_url": GOOD_BIG_VPN_URL,
                "url": "https://github.com/1andrevich/Re-filter-lists.git#community_ips.lst",
                "source_kind": "git_repo",
                "path": "community_ips.lst",
                "raw_text": "1.1.1.1/32\n",
            },
        ],
    )


def _bad_big_vpn_payload() -> RulesSourcePayload:
    return RulesSourcePayload(
        values=["example.com"],
        source_urls=[AGGREGATE_BIG_VPN_URL],
        version_name="big_vpn:aggregate",
        fetch_metadata=[
            {
                "configured_url": AGGREGATE_BIG_VPN_URL,
                "url": "https://github.com/1andrevich/Re-filter-lists.git#domains_all.lst",
                "source_kind": "git_repo",
                "path": "domains_all.lst",
                "raw_text": "example.com\n",
            },
            {
                "configured_url": AGGREGATE_BIG_VPN_URL,
                "url": "https://github.com/1andrevich/Re-filter-lists.git#ipsum.lst",
                "source_kind": "git_repo",
                "path": "ipsum.lst",
                "raw_text": "1.1.1.1/32\n",
            },
        ],
    )


def _invalid_big_vpn_payload() -> RulesSourcePayload:
    invalid_url = "git+https://github.com/1andrevich/Re-filter-lists.git?ref=main"
    return RulesSourcePayload(
        values=["example.com"],
        source_urls=[invalid_url],
        version_name="big_vpn:invalid",
        fetch_metadata=[
            {
                "configured_url": invalid_url,
                "url": "https://github.com/1andrevich/Re-filter-lists.git",
                "source_kind": "git_repo",
                "raw_text": "example.com\n",
            }
        ],
    )


def test_validate_manual_rules_accepts_russian_action_aliases() -> None:
    result = validate_manual_rules("ПРЯМО example.com\nВПН 8.8.8.8/32\n")

    assert result["valid"] is True
    assert result["errors"] == []
    assert result["normalized_text"] == "DIRECT example.com\nVPN 8.8.8.8/32\n"
    assert result["rules"][0]["action"] == "DIRECT"
    assert result["rules"][1]["action"] == "VPN"


def test_big_vpn_git_source_requires_explicit_paths() -> None:
    with pytest.raises(RulesSourceFetchError) as exc_info:
        RulesSourceAdapter._parse_git_source(
            "big_vpn",
            "git+https://github.com/1andrevich/Re-filter-lists.git?ref=main",
        )

    assert exc_info.value.code == "RULES_SOURCE_POLICY_VIOLATION"
    assert exc_info.value.details["policy_classification"] == "invalid"


def test_github_git_source_fetches_commit_date_without_clone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)

    class _FakeResponse:
        def __init__(self, body: bytes, *, headers: dict[str, str] | None = None, status: int = 200) -> None:
            self._body = body
            self.headers = headers or {}
            self.status = status

        def read(self, *_args: object, **_kwargs: object) -> bytes:
            return self._body

        def __enter__(self) -> "_FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def _fake_http_get(request: object, timeout: int | None = None) -> _FakeResponse:
        del timeout
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "api.github.com/repos/1andrevich/Re-filter-lists/commits/main" in url:
            return _FakeResponse(
                body=(
                    b'{"sha":"abc123","html_url":"https://github.com/1andrevich/Re-filter-lists/commit/abc123",'
                    b'"commit":{"author":{"date":"2026-06-02T10:11:12Z"}}}'
                ),
                headers={"Content-Type": "application/json"},
            )
        if "raw.githubusercontent.com/1andrevich/Re-filter-lists/abc123/community.lst" in url:
            return _FakeResponse(body=b"openai.com\n", headers={"Content-Type": "text/plain"})
        if "raw.githubusercontent.com/1andrevich/Re-filter-lists/abc123/community_ips.lst" in url:
            return _FakeResponse(body=b"1.1.1.1/32\n", headers={"Content-Type": "text/plain"})
        raise AssertionError(f"unexpected url: {url}")

    adapter = RulesSourceAdapter(http_get=_fake_http_get)
    monkeypatch.setattr(
        adapter,
        "_run_git",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("git clone fallback should not be used")),
    )

    payload = adapter._fetch_channel("big_vpn", [GOOD_BIG_VPN_URL])

    assert payload.version_name == "big_vpn:git:abc123"
    assert payload.values == ["openai.com", "1.1.1.1/32"]
    assert payload.fetch_metadata[0]["commit"] == "abc123"
    assert payload.fetch_metadata[0]["commit_date"] == "2026-06-02T10:11:12Z"
    assert payload.fetch_metadata[0]["path"] == "community.lst"


def test_rules_full_update_accepts_broad_big_vpn_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        "fwrouter_api.services.rules.DEFAULT_RULES_SOURCE_ADAPTER",
        _FakeRulesSourceAdapter(big_vpn_payload=_bad_big_vpn_payload()),
    )

    result = run_rules_full_update(_create_job())

    assert result["job_status"] == "success"
    overview = get_rules_overview()
    metadata = overview["artifacts"]["metadata"]
    fetch_summary = metadata["fetch_summary"]["big_vpn"]
    source_policy = fetch_summary["source_policy"]
    assert source_policy["valid"] is True
    assert source_policy["policy_classification"] == "broad_aggregate"
    assert source_policy["used_paths"] == ["domains_all.lst", "ipsum.lst"]


def test_rules_full_update_publishes_big_vpn_policy_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        "fwrouter_api.services.rules.DEFAULT_RULES_SOURCE_ADAPTER",
        _FakeRulesSourceAdapter(big_vpn_payload=_good_big_vpn_payload()),
    )

    result = run_rules_full_update(_create_job())
    overview = get_rules_overview()
    metadata = overview["artifacts"]["metadata"]
    fetch_summary = metadata["fetch_summary"]["big_vpn"]
    source_policy = fetch_summary["source_policy"]

    assert result["job_status"] == "success"
    assert source_policy["valid"] is True
    assert source_policy["policy_classification"] == "explicit_blacklist"
    assert source_policy["used_paths"] == ["community.lst", "community_ips.lst"]


def test_big_vpn_value_list_treats_plain_domains_as_domain_suffix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)

    validation = validate_value_list(
        "instagram.com\n1.1.1.1/32\n",
        action="VPN",
        source="big_vpn",
    )

    assert validation["valid"] is True
    assert any(
        rule["kind"] == "domain_suffix" and rule["value"] == ".instagram.com"
        for rule in validation["rules"]
    )
    assert any(rule["kind"] == "cidr" and rule["value"] == "1.1.1.1/32" for rule in validation["rules"])


def test_big_direct_value_list_treats_plain_domains_as_domain_suffix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)

    validation = validate_value_list(
        "cdninstagram.com\n",
        action="DIRECT",
        source="big_direct",
    )

    assert validation["valid"] is True
    assert validation["rules"][0]["kind"] == "domain_suffix"
    assert validation["rules"][0]["value"] == ".cdninstagram.com"


def test_big_vpn_value_list_compiles_subsumed_suffixes_and_collapses_cidrs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)

    validation = validate_value_list(
        "example.com\nfoo.example.com\n1.1.1.0/25\n1.1.1.128/25\n",
        action="VPN",
        source="big_vpn",
    )

    assert validation["valid"] is True
    assert validation["normalized_text"] == "1.1.1.0/24\n.example.com\n"
    assert validation["compile_stats"]["domain_suffix_subsumed_removed"] == 1
    assert validation["compile_stats"]["cidr_collapsed_removed"] == 1


def test_rules_full_update_preserves_last_good_artifacts_on_policy_violation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        "fwrouter_api.services.rules.DEFAULT_RULES_SOURCE_ADAPTER",
        _FakeRulesSourceAdapter(big_vpn_payload=_good_big_vpn_payload()),
    )

    first_result = run_rules_full_update(_create_job())
    first_texts = get_manual_rules_texts()
    good_big_vpn_text = first_texts["big_vpn_text"]
    good_metadata = first_texts["metadata"]

    monkeypatch.setattr(
        "fwrouter_api.services.rules.DEFAULT_RULES_SOURCE_ADAPTER",
        _FakeRulesSourceAdapter(big_vpn_payload=_invalid_big_vpn_payload()),
    )
    second_result = run_rules_full_update(_create_job())
    current_texts = get_manual_rules_texts()

    assert first_result["job_status"] == "success"
    assert second_result["job_status"] == "failed"
    assert second_result["error_code"] == "RULES_SOURCE_POLICY_VIOLATION"
    assert current_texts["big_vpn_text"] == good_big_vpn_text
    assert current_texts["metadata"] == good_metadata


def test_rules_full_update_fetch_failure_preserves_active_metadata_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        "fwrouter_api.services.rules.DEFAULT_RULES_SOURCE_ADAPTER",
        _FakeRulesSourceAdapter(big_vpn_payload=_good_big_vpn_payload()),
    )

    first_result = run_rules_full_update(_create_job())
    first_metadata = {
        item["ruleset_id"]: item
        for item in get_rules_overview()["metadata"]
    }

    monkeypatch.setattr(
        "fwrouter_api.services.rules.DEFAULT_RULES_SOURCE_ADAPTER",
        _FailingBigVpnRulesSourceAdapter(),
    )
    second_result = run_rules_full_update(_create_job())
    current_metadata = {
        item["ruleset_id"]: item
        for item in get_rules_overview()["metadata"]
    }

    assert first_result["job_status"] == "success"
    assert second_result["job_status"] == "failed"
    assert current_metadata["big_vpn"]["metadata_json"]["count"] == first_metadata["big_vpn"]["metadata_json"]["count"]
    assert current_metadata["effective"]["metadata_json"]["effective_counts"] == first_metadata["effective"]["metadata_json"]["effective_counts"]
    assert current_metadata["big_vpn"]["status"] == "active"
    assert current_metadata["big_vpn"]["last_error_code"] == "RULES_SOURCE_TIMEOUT"


def test_dataplane_manifest_bounds_heavy_effective_payloads() -> None:
    manifest = build_dataplane_manifest_from_state(
        plan_id="plan-1",
        reason="pytest",
        routing={"desired_mode": "direct", "applied_mode": "direct", "selective_default": "direct"},
        subjects=[
            {
                "subject_id": "sub-1",
                "subject_type": "docker",
                "display_name": "docker-1",
                "desired_mode": "direct",
                "runtime_state": "active",
                "is_active": True,
                "effective_state": {
                    "effective_mode": "direct",
                    "mode_source": "default",
                    "dataplane_path": "direct",
                    "selected_server_id": None,
                    "selected_server_source": None,
                    "runtime_enforcement": {"dataplane_capability": "nft_owned_table", "supported_modes": {"direct": True}},
                    "scoped_runtime": {
                        "state": "disabled",
                        "eligible": True,
                        "applied": False,
                        "raw_map": {"a": "b"},
                    },
                },
            }
        ],
        extra={
            "rules_effective": {
                "selective_default": "direct",
                "source_counts": {"manual": 1},
                "effective_counts": {"domains": 1},
                "runtime_enforcement": {"dataplane_capability": "nft_owned_table"},
                "raw_entries": list(range(200)),
            },
            "huge_map": {"k": "v"},
        },
    )

    assert manifest["extra"]["rules_effective"]["raw_entries"] == list(range(200))
    assert "rules" not in manifest["extra"]["rules_effective_summary"]
    assert "raw_entries" not in manifest["extra"]["rules_effective_summary"]
    assert manifest["subjects"][0]["scoped_runtime"]["state"] == "disabled"
    assert "raw_map" not in manifest["subjects"][0]["scoped_runtime"]
    assert manifest["summary"]["routing"]["desired_mode"] == "direct"
