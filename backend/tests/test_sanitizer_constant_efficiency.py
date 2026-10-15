from __future__ import annotations

import copy
import re
from concurrent.futures import ThreadPoolExecutor

from fwrouter_api.services import event_contract


def _legacy_sensitive_key(key: object) -> bool:
    """Frozen pre-optimization predicate for exact behavior comparison."""
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    if normalized in {
        re.sub(r"[^a-z0-9]", "", item)
        for item in event_contract._SAFE_CONTEXT_KEYS
    }:
        return False
    if normalized.endswith("id") and not any(
        marker in normalized
        for marker in ("token", "key", "secret", "password", "credential")
    ):
        return False
    return bool(event_contract._SENSITIVE_KEY.search(normalized)) or normalized in {
        "subscriptionuri", "subscriptionurl", "privatekey", "apikey", "accesskey",
        "secretkey", "clientsecret", "setcookie", "proxyauthorization", "clientuuid",
    }


class _DisplayKey:
    def __str__(self) -> str:
        return "Access.Token"


def test_sensitive_key_matches_frozen_legacy_predicate_across_key_variants() -> None:
    keys = [
        "request_id", "Request-ID", "REQUEST ID", "job.id", "apply_id", "event-id",
        "entity_id", "serverId", "connection id", "workflow_id", "causation-id",
        "correlation_id", "recovery_attempt_id", "attempt-id", "generation_id",
        "password", "PassWord", "pass-word", "access_token", "ACCESS-TOKEN",
        "refresh.token", "api_key", "private key", "clientSecret", "authorization",
        "set-cookie", "proxy_authorization", "subscription_uri", "subscription-url",
        "client_uuid", "token_id", "passwordId", "ordinary", "request_identifier",
        "café_token", "密钥", "ＡＰＩ＿ＫＥＹ", 7, None,
        ("token", "nested"), _DisplayKey(),
    ]
    assert all(
        event_contract._sensitive_key(key) == _legacy_sensitive_key(key)
        for key in keys
    )


def test_nested_sanitization_matches_legacy_key_classification(monkeypatch) -> None:
    source = {
        "request-id": "synthetic-request-001",
        "server_id": "synthetic-server-01",
        "password": "synthetic-password-canary",
        "nested": [
            {
                "Access.Token": "synthetic-access-token-canary",
                "ordinary_id": "synthetic-ordinary-id",
                "endpoint": (
                    "https://synthetic-user:synthetic-pass@example.invalid/path?"
                    "token=synthetic-query-canary"
                ),
                "headers": {
                    "Authorization": "Bearer synthetic-bearer-canary",
                    "api_key": "synthetic-api-key-canary",
                    "message": (
                        "private key -----BEGIN FAKE PRIVATE KEY-----"
                        "synthetic-private-canary-----END FAKE PRIVATE KEY-----"
                    ),
                },
            },
            {9: "synthetic-numeric-key", None: "synthetic-none-key"},
        ],
        "safe_unknown": "synthetic-visible-value",
    }
    export_object = {
        "schema_version": 2,
        "export_options": {"include_secrets": False, "scope": "synthetic"},
        "operational_events": [
            {
                "event_id": "synthetic-export-event-001",
                "event_context": {"request_id": "synthetic-export-request-001"},
                "details": {
                    "unknown_export_field": "synthetic-export-visible",
                    "refresh_token": "synthetic-export-token-canary",
                },
            }
        ],
    }
    expected = event_contract.sanitize_value(copy.deepcopy(source))
    expected_export = event_contract.sanitize_value(copy.deepcopy(export_object))
    with monkeypatch.context() as patcher:
        patcher.setattr(event_contract, "_sensitive_key", _legacy_sensitive_key)
        actual = event_contract.sanitize_value(copy.deepcopy(source))
        actual_export = event_contract.sanitize_value(copy.deepcopy(export_object))
    assert actual == expected
    assert actual_export == expected_export
    rendered = repr((actual, actual_export))
    for canary in (
        "synthetic-password-canary", "synthetic-access-token-canary",
        "synthetic-query-canary", "synthetic-bearer-canary", "synthetic-api-key-canary",
        "synthetic-private-canary", "synthetic-user:synthetic-pass",
        "synthetic-export-token-canary",
    ):
        assert canary not in rendered
    assert actual["request-id"] == "synthetic-request-001"
    assert actual["server_id"] == "synthetic-server-01"
    assert actual["safe_unknown"] == "synthetic-visible-value"
    assert actual_export["operational_events"][0]["event_id"] == "synthetic-export-event-001"
    assert (
        actual_export["operational_events"][0]["details"]["unknown_export_field"]
        == "synthetic-export-visible"
    )


def test_sensitive_key_uses_one_normalization_regex_call_per_key(monkeypatch) -> None:
    original_sub = re.sub
    calls = 0

    def counted_sub(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_sub(*args, **kwargs)

    monkeypatch.setattr(event_contract.re, "sub", counted_sub)
    for _ in range(500):
        event_contract._sensitive_key("request_id")
        event_contract._sensitive_key("access_token")
    assert calls == 1000


def test_constant_is_immutable_and_concurrent_classification_has_no_growth() -> None:
    normalized_safe_keys = event_contract._NORMALIZED_SAFE_CONTEXT_KEYS
    namespace_names = frozenset(vars(event_contract))
    corpus = [
        "request_id", "Request-ID", "access_token", "private-key", "job_id",
        "ordinary", "token_id", "correlation id", "authorization", "safe_id",
    ] * 100
    expected = [event_contract._sensitive_key(key) for key in corpus]
    with ThreadPoolExecutor(max_workers=12) as executor:
        concurrent = list(executor.map(event_contract._sensitive_key, corpus))
    assert concurrent == expected
    assert event_contract._NORMALIZED_SAFE_CONTEXT_KEYS is normalized_safe_keys
    assert isinstance(normalized_safe_keys, frozenset)
    assert len(normalized_safe_keys) == len(event_contract._SAFE_CONTEXT_KEYS)
    assert frozenset(vars(event_contract)) == namespace_names
