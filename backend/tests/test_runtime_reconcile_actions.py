from __future__ import annotations

import json
from pathlib import Path

from fwrouter_api.adapters.xray import RealXrayAdapter, XrayApplyResult
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    get_settings.cache_clear()


def _write_xray_config(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "inbounds": [
            {
                "port": 5300,
                "protocol": "vless",
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {"network": "ws", "wsSettings": {"path": "/vless"}},
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
    }
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_xray_reload_uses_restart_not_force_recreate(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    settings = get_settings()
    config_path = settings.paths.state_dir / "xray" / "config.json"
    compose_path = settings.paths.state_dir / "xray" / "docker-compose.yml"
    _write_xray_config(config_path)
    compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.write_text("services:\n  fwrouter-xray:\n    image: teddysun/xray\n", encoding="utf-8")

    calls: list[tuple[str, dict[str, object]]] = []

    def _runner(action: str, payload: dict[str, object]) -> XrayApplyResult:
        calls.append((action, dict(payload)))
        return XrayApplyResult(ok=True, message="ok", details={"action": action})

    adapter = RealXrayAdapter(
        config_path=config_path,
        compose_path=compose_path,
        log_root=tmp_path / "log" / "xray",
        runner=_runner,
    )

    result = adapter.reload()

    assert result.ok is True
    assert calls == [("reload", {})]
