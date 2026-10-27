from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from fwrouter_api.services import presentation_client_list_admission as admission


def _policy(path: Path, token: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "principal": "presentation-operator",
        "scopes": ["ui.client-list.write"],
        "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
        "canonical_origin": "https://router.example.test",
    }), encoding="utf-8")
    path.chmod(0o600)


def _test_app():
    app = FastAPI()

    @app.post("/write")
    def write(request: Request):
        with admission.verified_presentation_write(request):
            admission.require_presentation_write_grant()
            return {"ok": True}

    return app


def test_scoped_presentation_grant_is_request_bound_and_revoked(monkeypatch, tmp_path: Path):
    token = secrets.token_hex(32)
    verifier = tmp_path / "etc" / "fwrouter" / "presentation-client-list-verifier.json"
    _policy(verifier, token)
    monkeypatch.setattr(admission, "VERIFIER_PATH", verifier)
    monkeypatch.setattr(admission, "_trusted_uid", os.getuid)
    client = TestClient(_test_app(), client=("127.0.0.1", 45678),
                        headers={"Host": "router.example.test"})

    with client:
        accepted = client.post(
            "/write", headers={"Authorization": f"Bearer {token}",
                               "Origin": "https://router.example.test"},
        )
        assert accepted.status_code == 200
        with pytest.raises(admission.PresentationAdmissionError, match="PRESENTATION_WRITE_ADMISSION_REQUIRED"):
            admission.require_presentation_write_grant()


def test_verifier_path_rejects_symlink_and_group_writable_policy(monkeypatch, tmp_path: Path):
    token = secrets.token_hex(32)
    verifier = tmp_path / "etc" / "fwrouter" / "presentation-client-list-verifier.json"
    _policy(verifier, token)
    monkeypatch.setattr(admission, "VERIFIER_PATH", verifier)
    monkeypatch.setattr(admission, "_trusted_uid", os.getuid)
    assert admission._verifier()["principal"] == "presentation-operator"

    target = tmp_path / "real-policy"
    _policy(target, token)
    verifier.unlink()
    verifier.symlink_to(target)
    with pytest.raises(admission.PresentationAdmissionError, match="PRESENTATION_AUTH_CONFIG_UNAVAILABLE"):
        admission._verifier()

    verifier.unlink()
    _policy(verifier, token)
    verifier.chmod(0o640)
    with pytest.raises(admission.PresentationAdmissionError, match="PRESENTATION_AUTH_CONFIG_UNAVAILABLE"):
        admission._verifier()


def test_verifier_rejects_duplicate_json_members(monkeypatch, tmp_path: Path):
    verifier = tmp_path / "etc" / "fwrouter" / "presentation-client-list-verifier.json"
    verifier.parent.mkdir(mode=0o700, parents=True)
    verifier.write_text(
        '{"schema_version":1,"schema_version":1,"principal":"presentation-operator",'
        '"scopes":["ui.client-list.write"],"token_sha256":"' + "a" * 64 + '",'
        '"canonical_origin":"https://router.example.test"}',
        encoding="utf-8",
    )
    verifier.chmod(0o600)
    monkeypatch.setattr(admission, "VERIFIER_PATH", verifier)
    monkeypatch.setattr(admission, "_trusted_uid", os.getuid)
    with pytest.raises(admission.PresentationAdmissionError, match="PRESENTATION_AUTH_CONFIG_UNAVAILABLE"):
        admission._verifier()
