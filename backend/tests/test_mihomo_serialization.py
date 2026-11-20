from __future__ import annotations

import yaml

from fwrouter_api.services.mihomo_serialization import dump_mihomo_config, needs_quoted_string


def test_ambiguous_string_scalars_are_quoted_for_mihomo_yaml() -> None:
    payload = {
        "proxies": [{
            "name": "123e4567",
            "type": "vless",
            "reality-opts": {"short-id": "123e4567"},
            "server": "1e10.example",
            "label": "null",
        }]
    }

    dumped = dump_mihomo_config(payload)
    restored = yaml.safe_load(dumped)

    assert 'short-id: "123e4567"' in dumped
    assert restored == payload
    assert needs_quoted_string("123e4567") is True
    assert needs_quoted_string("ordinary.example") is False


def test_typed_numeric_boolean_and_null_values_keep_their_yaml_types() -> None:
    payload = {
        "number": 123,
        "enabled": True,
        "empty": None,
        "string_number": "123",
        "string_boolean": "true",
    }

    restored = yaml.safe_load(dump_mihomo_config(payload))

    assert restored == payload
    assert isinstance(restored["number"], int)
    assert isinstance(restored["enabled"], bool)
    assert restored["empty"] is None
    assert isinstance(restored["string_number"], str)
    assert isinstance(restored["string_boolean"], str)


def test_semantic_comparison_preserves_go_scalar_spelling_and_sequence_order(tmp_path) -> None:
    from fwrouter_api.services.mihomo_config_yaml import compare_mihomo_yaml_files

    active = tmp_path / "active.yaml"
    candidate = tmp_path / "candidate.yaml"
    active.write_text("name: yes\nport: 0123\nrules: [one, two]\n", encoding="utf-8")

    candidate.write_text("# format only\nrules:\n  - one\n  - two\nport: 0123\nname: yes\n", encoding="utf-8")
    assert compare_mihomo_yaml_files(active, candidate)

    for changed in (
        "name: true\nport: 0123\nrules: [one, two]\n",
        "name: yes\nport: 83\nrules: [one, two]\n",
        "name: yes\nport: 0123\nrules: [two, one]\n",
        "name: 'yes'\nport: 0123\nrules: [one, two]\n",
        "name: yes\nport: 0123\nrules: [one, two]\nName: other\n",
    ):
        candidate.write_text(changed, encoding="utf-8")
        assert not compare_mihomo_yaml_files(active, candidate)
