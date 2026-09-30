from __future__ import annotations

import json
from pathlib import Path

import fwrouter_api.adapters.subscription as adapter_module
from fwrouter_api.adapters.subscription import SubscriptionRefreshResult, SubscriptionRefreshStatus, SubscriptionServer
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import subscription
from fwrouter_api.services.server_state import ensure_routing_global_state


class _Adapter:
    def __init__(self, payloads: dict[str, tuple[str, ...] | None]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def refresh(self, url: str) -> SubscriptionRefreshResult:
        self.calls.append(url)
        names = self.payloads[url]
        if names is None:
            return SubscriptionRefreshResult(
                status=SubscriptionRefreshStatus.FAILED,
                servers=[],
                error_code="SUBSCRIPTION_DOWNLOAD_FAILED",
                error_message=f"Failed for {url}",
            )
        return SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.SUCCESS,
            servers=[
                SubscriptionServer(
                    server_id=name,
                    server_name=name,
                    provider_name="subscription",
                    raw={"name": name, "type": "vless"},
                )
                for name in names
            ],
            message="ok",
            metadata={"url": url},
        )


def _setup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_MAINTENANCE_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_WATCHDOG_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("FWROUTER_RUNTIME_CONVERGENCE_SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    initialize_database()
    ensure_routing_global_state()


def test_targeted_refresh_fetches_only_source_and_keeps_peer_inventory_and_membership(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    first = "https://one.example/sub?token=private"
    peer = "https://two.example/sub"
    adapter = _Adapter({first: ("first-old",), peer: ("peer-only",)})
    monkeypatch.setattr(adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)
    imported = subscription.refresh_subscription_inventory_batch([first, peer])
    assert imported["ok"] is True

    with db_session() as connection:
        peer_membership_before = dict(connection.execute(
            "SELECT * FROM subscription_server_memberships WHERE source_id=? AND server_id='peer-only'",
            (subscription._source_id(peer),),
        ).fetchone())
        connection.execute(
            "UPDATE servers SET server_name='peer-retained-name' WHERE server_id='peer-only'"
        )
    adapter.calls.clear()
    adapter.payloads[first] = ("first-new",)

    refreshed = subscription.refresh_subscription(subscription._source_id(first))

    assert adapter.calls == [first]
    assert refreshed["ok"] is True
    assert refreshed["batch"]["targeted"] is True
    assert refreshed["batch"]["targeted_source_ref"] == subscription._source_id(first)
    with db_session() as connection:
        peer_row = connection.execute(
            "SELECT server_name, inventory_state FROM servers WHERE server_id='peer-only'"
        ).fetchone()
        peer_membership_after = dict(connection.execute(
            "SELECT * FROM subscription_server_memberships WHERE source_id=? AND server_id='peer-only'",
            (subscription._source_id(peer),),
        ).fetchone())
        first_active = connection.execute(
            "SELECT inventory_state FROM servers WHERE server_id='first-new'"
        ).fetchone()
    assert peer_row["server_name"] == "peer-retained-name"
    assert peer_row["inventory_state"] == "active"
    assert peer_membership_after == peer_membership_before
    assert first_active["inventory_state"] == "active"


def test_targeted_source_failure_retains_existing_last_good_but_new_failed_intent_is_saved(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    first = "https://one.example/sub"
    peer = "https://two.example/sub"
    adapter = _Adapter({first: ("first-good",), peer: ("peer-good",)})
    monkeypatch.setattr(adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)
    subscription.refresh_subscription_inventory_batch([first, peer])

    adapter.calls.clear()
    adapter.payloads[first] = None
    failed = subscription.refresh_subscription(subscription._source_id(first))
    assert adapter.calls == [first]
    assert failed["ok"] is False
    rows = failed["state"]["metadata"]["subscriptions"]["items"]
    first_row = next(item for item in rows if item["url"] == first)
    peer_row = next(item for item in rows if item["url"] == peer)
    assert first_row["used_last_good"] is True
    assert peer_row["used_last_good"] is False

    new_source = "https://new.example/sub"
    adapter.payloads[new_source] = None
    saved_failure = subscription.refresh_subscription_inventory_batch([new_source])
    state = saved_failure["state"]
    assert any(item["url"] == new_source for item in state["metadata"]["subscriptions"]["items"])
    new_row = next(item for item in state["metadata"]["subscriptions"]["items"] if item["url"] == new_source)
    assert new_row["used_last_good"] is False


def test_full_refresh_fetches_every_saved_source_and_safe_labels_hide_url_secrets(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    first = "https://user:pass@same.example/feed/alpha?token=abc#fragment"
    second = "https://same.example/feed/beta?key=def"
    adapter = _Adapter({first: ("alpha",), second: ("beta",)})
    monkeypatch.setattr(adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", adapter)
    subscription.refresh_subscription_inventory_batch([first, second])
    adapter.calls.clear()

    result = subscription.refresh_all_subscriptions()
    labels = subscription.safe_subscription_source_labels(result["state"]["metadata"])

    assert result["ok"] is True
    assert set(adapter.calls) == {first, second}
    assert len(labels) == 2
    rendered = " ".join(labels.values())
    assert "https://same.example/… (1)" in rendered
    assert "https://same.example/… (2)" in rendered
    for secret in ("user", "pass", "feed", "alpha", "beta", "token", "abc", "key", "def", "fragment", "src:"):
        assert secret not in rendered

    public = subscription.compact_subscription_metadata(result["state"]["metadata"], redact_urls=True)
    assert all("source_ref" in item and item["display_label"] in labels.values() for item in public["subscriptions"]["items"])
    assert "same.example/feed" not in json.dumps(public)

