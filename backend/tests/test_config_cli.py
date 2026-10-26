from __future__ import annotations

from pathlib import Path

import pytest

from fwrouter_api.services import config_cli


def test_credential_file_accepts_private_file_under_sticky_tmp(tmp_path: Path):
    token = "a" * 64
    credential_dir = tmp_path / "owned"
    credential_dir.mkdir(mode=0o700)
    credential = credential_dir / "credential"
    credential.write_text(token, encoding="ascii")
    credential.chmod(0o600)

    assert config_cli._read_credential(str(credential)) == token


def test_credential_file_rejects_symlink_and_unsafe_parent(tmp_path: Path):
    token = "b" * 64
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "credential"
    target.write_text(token, encoding="ascii")
    target.chmod(0o600)
    link = private / "link"
    link.symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        config_cli._read_credential(str(link))

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    exposed = unsafe / "credential"
    exposed.write_text(token, encoding="ascii")
    exposed.chmod(0o600)
    with pytest.raises(ValueError, match="CREDENTIAL_PATH_NOT_PRIVATE"):
        config_cli._read_credential(str(exposed))


def test_cli_response_validation_is_typed_and_binds_etag():
    document = {"ok": True, "data": {"client_list": {
        "show_inactive": False,
        "etag": '"cli1-' + "a" * 64 + '"',
        "compatibility_status": "current",
        "provenance": "default",
        "incarnation": None,
        "revision": None,
    }}}
    validated = config_cli._validated_resource(document, document["data"]["client_list"]["etag"])
    assert validated["show_inactive"] is False
    assert validated["resource"] == "ui-client-list"

    document["data"]["client_list"]["show_inactive"] = "false"
    with pytest.raises(ValueError, match="API_RESPONSE_INVALID"):
        config_cli._validated_resource(document, document["data"]["client_list"]["etag"])

    document["data"]["client_list"]["show_inactive"] = False
    with pytest.raises(ValueError, match="API_RESPONSE_INVALID"):
        config_cli._validated_resource(document, '"cli1-' + "b" * 64 + '"')


def test_cli_does_not_echo_unrecognized_api_error_text():
    assert config_cli._safe_error({"error": {"code": "TOKEN_" + "c" * 64}}) == "API_REQUEST_FAILED"
    assert config_cli._safe_error({"error": {"code": ["arbitrary"]}}) == "API_REQUEST_FAILED"


def test_global_help_exposes_configuration_client_without_running_operation(capsys):
    from fwrouter_api.cli import main

    with pytest.raises(SystemExit) as stopped:
        main(["--help"])
    assert stopped.value.code == 0
    assert "config" in capsys.readouterr().out
