from __future__ import annotations

import os
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from fwrouter_api.adapters import mihomo as mihomo_module
from fwrouter_api.adapters.mihomo import MihomoHttpAdapter
from fwrouter_api.services import mihomo_config_yaml


def test_lazy_line_iterator_matches_str_splitlines_boundaries() -> None:
    boundaries = ["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
    values = ["", "x", "\n", "x\n", "x\n\n", "\r\n", "a\r\nb", "a\n\rb"]
    values.extend(f"left{boundary}right" for boundary in boundaries)
    values.extend(f"left{boundary}{next_boundary}right" for boundary in boundaries for next_boundary in boundaries)
    rng = random.Random(20261016)
    alphabet = ["a", " ", "\t", *boundaries]
    values.extend("".join(rng.choice(alphabet) for _ in range(rng.randrange(40))) for _ in range(250))
    values.append("mixed-port: 7890\n" + "  - synthetic-rule\n" * 3000)
    values.append("x" * (40 * 1024) + "\u2028tail\n")

    for text in values:
        assert list(mihomo_module._iter_config_lines(text)) == text.splitlines()


def test_configured_scanners_match_legacy_splitlines_reference(monkeypatch, tmp_path: Path) -> None:
    text = (
        "secret: synthetic-secret\u2028"
        "mixed-port: 7890\r\n"
        "bind-address: 0.0.0.0\x85"
        "tun:\n  enable: true\v"
        "listeners:\n"
        "  - name: fwrouter-redir\r"
        "    type: redir\n    listen: 0.0.0.0\n    port: 7892\n"
        "  - name: fwrouter-tproxy\n"
        "    type: tproxy\n    listen: 0.0.0.0\n    port: 7893\n"
        "proxies:\n  - synthetic\u2029"
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(text, encoding="utf-8")
    adapter = MihomoHttpAdapter(config_path=config_path, contours_path=tmp_path / "missing.yaml")
    adapter.check_port = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    optimized = {
        "scalar": adapter._config_top_level_scalar("secret", text=text),
        "ports": adapter._scan_listener_ports(text=text),
        "listeners": adapter._scan_transparent_listeners(text=text),
        "tun": adapter._scan_tun_enabled(text=text),
        "details": adapter._config_runtime_details(config_text=text),
    }

    monkeypatch.setattr(mihomo_module, "_iter_config_lines", lambda source: iter(source.splitlines()))
    reference = {
        "scalar": adapter._config_top_level_scalar("secret", text=text),
        "ports": adapter._scan_listener_ports(text=text),
        "listeners": adapter._scan_transparent_listeners(text=text),
        "tun": adapter._scan_tun_enabled(text=text),
        "details": adapter._config_runtime_details(config_text=text),
    }
    assert optimized == reference


def test_late_headers_use_legacy_splitlines_path_with_equivalent_output(monkeypatch, tmp_path: Path) -> None:
    text = "# padding\n" * 6000 + (
        "secret: synthetic-secret\n"
        "mixed-port: 7890\n"
        "bind-address: 0.0.0.0\n"
        "tun:\n  enable: true\n"
        "listeners:\n"
        "  - name: fwrouter-redir\n    type: redir\n    listen: 0.0.0.0\n    port: 7892\n"
        "proxies:\n"
    )
    assert text.find("listeners:") > 32 * 1024
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    adapter = MihomoHttpAdapter(config_path=path, contours_path=tmp_path / "missing.yaml")
    adapter.check_port = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    optimized = adapter._config_runtime_details(config_text=text)
    monkeypatch.setattr(mihomo_module, "_iter_config_lines", lambda source: iter(source.splitlines()))
    reference = adapter._config_runtime_details(config_text=text)
    assert optimized == reference


def test_literal_absence_guards_preserve_nested_and_commented_values(monkeypatch, tmp_path: Path) -> None:
    text = (
        "# listeners:\n"
        "# tun:\n"
        "nested:\n"
        "  bind-address: 0.0.0.0\n"
        "  listeners:\n"
        "    - name: fwrouter-redir\n"
        "      type: redir\n"
    )
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    adapter = MihomoHttpAdapter(config_path=path, contours_path=tmp_path / "missing.yaml")
    adapter.check_port = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    optimized = {
        "scalar": adapter._config_top_level_scalar("bind-address", text=text),
        "ports": adapter._scan_listener_ports(text=text),
        "listeners": adapter._scan_transparent_listeners(text=text),
        "tun": adapter._scan_tun_enabled(text=text),
        "details": adapter._config_runtime_details(config_text=text),
    }
    assert optimized["scalar"] is None
    assert optimized["ports"] == {"mixed_port": None, "tproxy_port": None, "redir_port": None}
    assert optimized["listeners"] == {}
    assert optimized["tun"] is False
    monkeypatch.setattr(mihomo_module, "_iter_config_lines", lambda source: iter(source.splitlines()))
    reference = {
        "scalar": adapter._config_top_level_scalar("bind-address", text=text),
        "ports": adapter._scan_listener_ports(text=text),
        "listeners": adapter._scan_transparent_listeners(text=text),
        "tun": adapter._scan_tun_enabled(text=text),
        "details": adapter._config_runtime_details(config_text=text),
    }
    assert optimized == reference


def test_missing_configured_literals_skip_line_materialization(monkeypatch, tmp_path: Path) -> None:
    adapter = MihomoHttpAdapter(config_path=tmp_path / "missing.yaml", contours_path=tmp_path / "missing-contours.yaml")

    def should_not_iterate(_source: str):
        raise AssertionError("a missing literal cannot match the existing scanner")

    monkeypatch.setattr(mihomo_module, "_iter_config_lines", should_not_iterate)
    assert adapter._config_top_level_scalar("secret", text="mode: rule\n") is None
    assert adapter._scan_listener_ports(text="mode: rule\n") == {
        "mixed_port": None, "tproxy_port": None, "redir_port": None,
    }
    assert adapter._scan_transparent_listeners(text="mode: rule\n") == {}
    assert adapter._scan_tun_enabled(text="mode: rule\n") is False


def test_comment_and_nested_markers_take_legacy_splitlines_path(monkeypatch, tmp_path: Path) -> None:
    text = (
        "# secret: ignored\n"
        "# listeners: ignored\n"
        "# tun: ignored\n"
        "nested:\n"
        "  bind-address: 0.0.0.0\n"
        "  listeners:\n"
        "    - name: fwrouter-redir\n"
        "      type: redir\n"
        "  tun:\n"
        "    enable: true\n"
        "# pad the missing-header scan past the lazy prefix bound\n"
        + "# padding\n" * 5000
    )
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    adapter = MihomoHttpAdapter(config_path=path, contours_path=tmp_path / "missing.yaml")

    def lazy_path_is_unexpected(_source: str):
        raise AssertionError("comment or nested marker must use splitlines fallback")

    monkeypatch.setattr(mihomo_module, "_iter_config_lines", lazy_path_is_unexpected)
    assert adapter._config_top_level_scalar("secret", text=text) is None
    assert adapter._config_top_level_scalar("bind-address", text=text) is None
    assert adapter._scan_listener_ports(text=text) == {
        "mixed_port": None, "tproxy_port": None, "redir_port": None,
    }
    assert adapter._scan_transparent_listeners(text=text) == {}
    assert adapter._scan_tun_enabled(text=text) is False


def test_config_text_reads_changes_deletion_recreation_and_atomic_replace(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    replacement = tmp_path / "config.next.yaml"
    adapter = MihomoHttpAdapter(config_path=path, contours_path=tmp_path / "missing.yaml")

    assert adapter._config_text() == ""
    path.write_text("mixed-port: 1001\n", encoding="utf-8")
    assert adapter._config_top_level_scalar("mixed-port") == "1001"
    path.write_text("mixed-port: 2002\n", encoding="utf-8")
    assert adapter._config_top_level_scalar("mixed-port") == "2002"
    path.unlink()
    assert adapter._config_text() == ""
    path.write_text("mixed-port: 3003\n", encoding="utf-8")
    assert adapter._config_top_level_scalar("mixed-port") == "3003"
    replacement.write_text("mixed-port: 4004\n", encoding="utf-8")
    os.replace(replacement, path)
    assert adapter._config_top_level_scalar("mixed-port") == "4004"


def test_configured_metadata_snapshot_reads_complete_new_file_after_preopen_replace(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    replacement = tmp_path / "config.next.yaml"
    path.write_text("mixed-port: 5101\n", encoding="utf-8")
    replacement.write_text("mixed-port: 6202\n", encoding="utf-8")
    adapter = MihomoHttpAdapter(config_path=path, contours_path=tmp_path / "missing.yaml")
    original_open = mihomo_config_yaml.os.open
    replaced = False

    def replace_before_open(target: str | bytes | os.PathLike[str], flags: int, *args: object) -> int:
        nonlocal replaced
        if os.fspath(target) == os.fspath(path) and not replaced:
            replaced = True
            os.replace(replacement, path)
        return original_open(target, flags, *args)

    monkeypatch.setattr(mihomo_config_yaml.os, "open", replace_before_open)
    assert adapter._config_top_level_scalar("mixed-port") == "6202"


def test_configured_metadata_snapshot_keeps_open_inode_across_atomic_replace(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    replacement = tmp_path / "config.next.yaml"
    path.write_text("mixed-port: 5101\n", encoding="utf-8")
    replacement.write_text("mixed-port: 6202\n", encoding="utf-8")
    adapter = MihomoHttpAdapter(config_path=path, contours_path=tmp_path / "missing.yaml")
    original_read = mihomo_config_yaml.os.read
    replaced = False

    def replace_after_first_read(descriptor: int, count: int) -> bytes:
        nonlocal replaced
        chunk = original_read(descriptor, count)
        if chunk and not replaced:
            replaced = True
            os.replace(replacement, path)
        return chunk

    monkeypatch.setattr(mihomo_config_yaml.os, "read", replace_after_first_read)
    # The descriptor is a complete old-config snapshot even though the path
    # has already moved to its new complete config.
    assert adapter._config_top_level_scalar("mixed-port") == "5101"
    assert adapter._config_top_level_scalar("mixed-port") == "6202"


def test_metadata_snapshot_rejects_inplace_mutation_during_read(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("mixed-port: 5101\n", encoding="utf-8")
    original_read = mihomo_config_yaml.os.read
    mutated = False

    def mutate_after_read(descriptor: int, count: int) -> bytes:
        nonlocal mutated
        chunk = original_read(descriptor, count)
        if chunk and not mutated:
            mutated = True
            before = path.stat()
            with path.open("r+b") as stream:
                stream.write(b"mixed-port: 6202\n")
                stream.flush()
            # Make the modification-time delta deterministic even on filesystems
            # whose timestamp clock can report consecutive writes identically.
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        return chunk

    monkeypatch.setattr(mihomo_config_yaml.os, "read", mutate_after_read)
    with pytest.raises(mihomo_config_yaml.MihomoYamlInvalidError, match="file_changed_during_read"):
        mihomo_config_yaml.read_mihomo_yaml_snapshot_bytes(path)


def test_metadata_snapshot_rejects_symlink_and_oversize(tmp_path: Path) -> None:
    target = tmp_path / "target.yaml"
    target.write_text("mode: rule\n", encoding="utf-8")
    symlink = tmp_path / "linked.yaml"
    symlink.symlink_to(target)
    with pytest.raises(mihomo_config_yaml.MihomoYamlInvalidError):
        mihomo_config_yaml.read_mihomo_yaml_snapshot_bytes(symlink)
    with pytest.raises(mihomo_config_yaml.MihomoYamlLimitError, match="byte_limit"):
        mihomo_config_yaml.read_mihomo_yaml_snapshot_bytes(target, max_bytes=4)


def test_adapter_config_text_keeps_strict_path_fence_during_atomic_replace(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    replacement = tmp_path / "config.next.yaml"
    path.write_text("mixed-port: 5101\n", encoding="utf-8")
    replacement.write_text("mixed-port: 6202\n", encoding="utf-8")
    adapter = MihomoHttpAdapter(config_path=path, contours_path=tmp_path / "missing.yaml")
    original_open = mihomo_config_yaml.os.open
    replaced = False

    def replace_before_strict_open(target: str | bytes | os.PathLike[str], flags: int, *args: object) -> int:
        nonlocal replaced
        if os.fspath(target) == os.fspath(path) and not replaced:
            replaced = True
            os.replace(replacement, path)
        return original_open(target, flags, *args)

    monkeypatch.setattr(mihomo_config_yaml.os, "open", replace_before_strict_open)
    with pytest.raises(mihomo_config_yaml.MihomoYamlInvalidError, match="file_changed_before_read"):
        adapter._config_text()


def test_mihomo_health_uses_strict_config_reader_not_metadata_snapshot(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("mode: rule\n", encoding="utf-8")
    adapter = MihomoHttpAdapter(config_path=path, contours_path=tmp_path / "missing.yaml")

    def strict_reader_rejects(_path: Path) -> str:
        raise mihomo_config_yaml.MihomoYamlInvalidError("strict_config_read_failed")

    def metadata_reader_must_not_run(_path: Path) -> str:
        raise AssertionError("health must not use the observational metadata snapshot")

    monkeypatch.setattr(mihomo_module, "read_mihomo_yaml_text", strict_reader_rejects)
    monkeypatch.setattr(mihomo_module, "read_mihomo_yaml_snapshot_text", metadata_reader_must_not_run)
    health = adapter.health()
    assert health.runtime_state is mihomo_module.MihomoRuntimeState.DEGRADED
    assert health.details["error"] == "strict_config_read_failed"


def test_concurrent_atomic_replacements_return_complete_per_read_configs(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    next_path = tmp_path / "config.next.yaml"
    path.write_text("mixed-port: 5101\n", encoding="utf-8")
    adapter = MihomoHttpAdapter(config_path=path, contours_path=tmp_path / "missing.yaml")
    values = {"5101", "6202"}
    assert adapter._config_top_level_scalar("mixed-port") == "5101"
    published = threading.Event()
    observed = threading.Event()
    reads: list[str | None] = [adapter._config_top_level_scalar("mixed-port")]

    def replace_configs() -> None:
        next_path.write_text("mixed-port: 6202\n", encoding="utf-8")
        os.replace(next_path, path)
        published.set()
        assert observed.wait(timeout=5)
        for index in range(80):
            port = "6202" if index % 2 == 0 else "5101"
            next_path.write_text(f"mixed-port: {port}\n", encoding="utf-8")
            os.replace(next_path, path)

    def read_configs() -> None:
        assert published.wait(timeout=5)
        reads.append(adapter._config_top_level_scalar("mixed-port"))
        observed.set()
        for _ in range(160):
            reads.append(adapter._config_top_level_scalar("mixed-port"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(replace_configs)
        reader = executor.submit(read_configs)
        writer.result()
        reader.result()
    assert reads[:2] == ["5101", "6202"]
    assert set(reads) <= values
    assert "5101" in reads and "6202" in reads


def test_adapter_instances_read_independent_config_files(tmp_path: Path) -> None:
    first_path = tmp_path / "first.yaml"
    second_path = tmp_path / "second.yaml"
    first_path.write_text("mixed-port: 7001\n", encoding="utf-8")
    second_path.write_text("mixed-port: 8002\n", encoding="utf-8")
    first = MihomoHttpAdapter(config_path=first_path, contours_path=tmp_path / "missing.yaml")
    second = MihomoHttpAdapter(config_path=second_path, contours_path=tmp_path / "missing.yaml")

    assert first._config_top_level_scalar("mixed-port") == "7001"
    assert second._config_top_level_scalar("mixed-port") == "8002"
    first_path.write_text("mixed-port: 9003\n", encoding="utf-8")
    assert first._config_top_level_scalar("mixed-port") == "9003"
    assert second._config_top_level_scalar("mixed-port") == "8002"


def test_native_socket_observation_remains_fresh_for_each_configured_read(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "bind-address: 0.0.0.0\n"
        "listeners:\n"
        "  - name: fwrouter-redir\n    type: redir\n    listen: 0.0.0.0\n    port: 7892\n"
        "  - name: fwrouter-tproxy\n    type: tproxy\n    listen: 0.0.0.0\n    port: 7893\n",
        encoding="utf-8",
    )
    adapter = MihomoHttpAdapter(config_path=config_path, contours_path=tmp_path / "missing.yaml")
    observed: list[tuple[int, str, float]] = []

    def check_port(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
        observed.append((port, host, timeout))
        return True

    adapter.check_port = check_port  # type: ignore[method-assign]
    for _ in range(3):
        adapter._config_runtime_details()
    assert [port for port, _host, _timeout in observed] == [7892, 7893] * 3
