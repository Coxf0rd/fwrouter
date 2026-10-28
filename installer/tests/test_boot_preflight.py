from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "host/libexec/fwrouter/fwrouter-boot-preflight.sh"
SYSCTL_KEYS = (
    ("net.ipv4.ip_forward", "1"),
    ("net.ipv4.conf.all.rp_filter", "0"),
    ("net.ipv4.conf.default.rp_filter", "0"),
    ("net.ipv4.conf.all.src_valid_mark", "1"),
)


def _replace_once(source: str, old: str, new: str, expected: int = 1) -> str:
    actual = source.count(old)
    assert actual == expected, f"test fixture relocation count changed for {old!r}: {actual}"
    return source.replace(old, new)


@pytest.fixture
def preflight(tmp_path: Path) -> dict[str, object]:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    bin_dir = root / "bin"
    bin_dir.mkdir(mode=0o700)
    (bin_dir / "mkdir").symlink_to("/bin/mkdir")
    (bin_dir / "cat").symlink_to("/bin/cat")

    helper = root / "fwrouter-boot-preflight.sh"
    source = SOURCE.read_text(encoding="utf-8")
    bootstrap = root / "bootstrap-state.sh"
    rt_tables_dir = root / "etc/iproute2/rt_tables.d"
    tun_path = root / "dev/net/tun"
    source = _replace_once(
        source,
        "/opt/fwrouter-api/scripts/bootstrap-state.sh",
        str(bootstrap),
    )
    source = _replace_once(
        source,
        'RT_TABLES_DIR="/etc/iproute2/rt_tables.d"',
        f'RT_TABLES_DIR="{rt_tables_dir}"',
    )
    source = _replace_once(source, "/dev/net/tun", str(tun_path), expected=2)
    helper.write_text(source, encoding="utf-8")
    helper.chmod(0o700)

    sysctl_log = root / "sysctl.log"
    bootstrap_log = root / "bootstrap.log"
    command_log = root / "commands.log"
    unrelated_sysctl_dir = root / "etc/sysctl.d"
    unrelated_sysctl_dir.mkdir(parents=True)
    (unrelated_sysctl_dir / "50-unrelated.conf").write_text(
        "kernel.fwrouter_test_unrelated=invalid\n", encoding="utf-8",
    )
    sysctl = bin_dir / "sysctl"
    sysctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$SYSCTL_LOG\"\n"
        "[ \"$#\" -eq 2 ] && [ \"$1\" = '-n' ] || exit 90\n"
        "key=$2\n"
        "if [ \"${SYSCTL_MODE:-ok}\" = fail_key ] && [ \"$key\" = \"$SYSCTL_FAIL_KEY\" ]; then\n"
        "  exit 4\n"
        "fi\n"
        "if [ \"${SYSCTL_MODE:-ok}\" = read_error ] && [ \"$key\" = \"$SYSCTL_FAIL_KEY\" ]; then\n"
        "  exit 5\n"
        "fi\n"
        "if [ \"$key\" = \"${SYSCTL_FAIL_KEY:-}\" ]; then\n"
        "  case \"$SYSCTL_MODE\" in\n"
        "    empty) exit 0 ;;\n"
        "    multiline) printf '1\\n0\\n'; exit 0 ;;\n"
        "    malformed) printf 'not-a-value\\n'; exit 0 ;;\n"
        "    wrong) [ \"$SYSCTL_EXPECTED\" = 1 ] && printf '0\\n' || printf '1\\n'; exit 0 ;;\n"
        "  esac\n"
        "fi\n"
        "case \"$key\" in\n"
        "  net.ipv4.ip_forward|net.ipv4.conf.all.src_valid_mark) printf '1\\n' ;;\n"
        "  net.ipv4.conf.all.rp_filter|net.ipv4.conf.default.rp_filter) printf '0\\n' ;;\n"
        "  *) exit 6 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    sysctl.chmod(0o700)
    for command in ("nft", "ip"):
        path = bin_dir / command
        path.write_text(
            "#!/bin/sh\n"
            f"printf '{command}:%s\\n' \"$*\" >> \"$COMMAND_LOG\"\n",
            encoding="utf-8",
        )
        path.chmod(0o700)

    bootstrap.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$BOOTSTRAP_LOG\"\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o700)

    state_root = root / "state"
    log_root = root / "logs"
    run_root = root / "run"
    return {
        "root": root,
        "bin": bin_dir,
        "helper": helper,
        "bootstrap": bootstrap,
        "sysctl_log": sysctl_log,
        "bootstrap_log": bootstrap_log,
        "command_log": command_log,
        "unrelated_sysctl_file": unrelated_sysctl_dir / "50-unrelated.conf",
        "state_root": state_root,
        "log_root": log_root,
        "run_root": run_root,
        "rt_tables_dir": rt_tables_dir,
        "tun_path": tun_path,
    }


def _run(preflight: dict[str, object], **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": str(preflight["bin"]),
        "FWROUTER_STATE_ROOT": str(preflight["state_root"]),
        "FWROUTER_LOG_ROOT": str(preflight["log_root"]),
        "FWROUTER_RUN_ROOT": str(preflight["run_root"]),
        "SYSCTL_LOG": str(preflight["sysctl_log"]),
        "BOOTSTRAP_LOG": str(preflight["bootstrap_log"]),
        "COMMAND_LOG": str(preflight["command_log"]),
        "SYSCTL_MODE": "ok",
        **overrides,
    }
    return subprocess.run(
        [str(preflight["helper"])],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_preflight_reads_only_four_owned_values_and_is_repeatable(
    preflight: dict[str, object],
) -> None:
    # The managed persistent fragment is deliberately absent, and an unrelated
    # invalid setting exists. Runtime admission reads effective values only.
    assert Path(preflight["unrelated_sysctl_file"]).is_file()
    result = _run(preflight)
    assert result.returncode == 0, result.stderr
    assert _lines(preflight["sysctl_log"]) == [
        f"-n {key}" for key, _value in SYSCTL_KEYS
    ]
    assert len(_lines(preflight["bootstrap_log"])) == 1
    assert (preflight["rt_tables_dir"] / "fwrouter.conf").read_text() == "100 fwrouter_vpn\n"
    assert (Path(preflight["bin"]) / "nft").is_file()
    assert (Path(preflight["bin"]) / "ip").is_file()
    assert not Path(preflight["command_log"]).exists()

    repeated = _run(preflight)
    assert repeated.returncode == 0, repeated.stderr
    assert _lines(preflight["sysctl_log"]) == [
        f"-n {key}" for _attempt in range(2) for key, _value in SYSCTL_KEYS
    ]
    assert len(_lines(preflight["bootstrap_log"])) == 2


@pytest.mark.parametrize(("key", "expected"), SYSCTL_KEYS)
def test_preflight_rejects_each_wrong_managed_value_before_filesystem_effects(
    preflight: dict[str, object], key: str, expected: str,
) -> None:
    result = _run(
        preflight,
        SYSCTL_MODE="wrong",
        SYSCTL_FAIL_KEY=key,
        SYSCTL_EXPECTED=expected,
    )
    assert result.returncode != 0
    assert f"sysctl mismatch key={key} expected={expected}" in result.stderr
    assert "actual=" in result.stderr
    assert not Path(preflight["state_root"]).exists()
    assert not Path(preflight["log_root"]).exists()
    assert not Path(preflight["run_root"]).exists()
    assert not Path(preflight["rt_tables_dir"]).exists()
    assert not Path(preflight["bootstrap_log"]).exists()


@pytest.mark.parametrize(
    ("mode", "expected_detail"),
    (("fail_key", "read failed"), ("read_error", "read failed"),
     ("empty", "actual=empty"), ("multiline", "actual=invalid"),
     ("malformed", "actual=invalid")),
)
def test_preflight_fails_closed_on_unreadable_or_invalid_sysctl_output(
    preflight: dict[str, object], mode: str, expected_detail: str,
) -> None:
    key, expected = SYSCTL_KEYS[0]
    result = _run(preflight, SYSCTL_MODE=mode, SYSCTL_FAIL_KEY=key)
    assert result.returncode != 0
    assert f"key={key} expected={expected}" in result.stderr
    assert expected_detail in result.stderr
    assert not Path(preflight["state_root"]).exists()
    assert not Path(preflight["rt_tables_dir"]).exists()


def test_preflight_requires_sysctl_executable_before_side_effects(
    preflight: dict[str, object],
) -> None:
    (Path(preflight["bin"]) / "sysctl").unlink()
    result = _run(preflight)
    key, expected = SYSCTL_KEYS[0]
    assert result.returncode != 0
    message = f"sysctl tool unavailable key={key} expected={expected} actual=unreadable"
    assert message in result.stderr
    assert not Path(preflight["state_root"]).exists()
    assert not Path(preflight["rt_tables_dir"]).exists()
    assert not Path(preflight["bootstrap_log"]).exists()


@pytest.mark.parametrize("missing", ["ip", "nft"])
def test_preflight_preserves_tool_requirements_after_kernel_admission(
    preflight: dict[str, object], missing: str,
) -> None:
    (Path(preflight["bin"]) / missing).unlink()
    result = _run(preflight)
    assert result.returncode != 0
    assert f"{missing} command is not available" in result.stderr
    assert _lines(preflight["sysctl_log"]) == [f"-n {key}" for key, _value in SYSCTL_KEYS]
    assert len(_lines(preflight["bootstrap_log"])) == 1


def test_preflight_preserves_optional_tun_gate(preflight: dict[str, object]) -> None:
    result = _run(preflight, FWROUTER_REQUIRE_TUN="1")
    assert result.returncode != 0
    assert "is not available" in result.stderr
    assert not Path(preflight["sysctl_log"]).exists()
    assert not Path(preflight["state_root"]).exists()

    Path(preflight["tun_path"]).parent.mkdir(parents=True)
    Path(preflight["tun_path"]).symlink_to("/dev/null")
    allowed = _run(preflight, FWROUTER_REQUIRE_TUN="1")
    assert allowed.returncode == 0, allowed.stderr
    assert _lines(preflight["sysctl_log"]) == [f"-n {key}" for key, _value in SYSCTL_KEYS]
