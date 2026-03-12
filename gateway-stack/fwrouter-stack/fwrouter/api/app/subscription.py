from __future__ import annotations

from pathlib import Path
from typing import Dict, Any
import yaml

# Active mihomo config (mihomo2 setup)
MAIN_CONFIG = Path("/etc/fwrouter/mihomo2/config.yaml")


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    return data if isinstance(data, dict) else {}


def _write_yaml(path: Path, data: Dict[str, Any]) -> None:
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=False)
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")


def get_subscription() -> Dict[str, Any]:
    data = _load_yaml(MAIN_CONFIG)
    sub = (
        data.get("proxy-providers", {})
        .get("subscription", {})
    )
    return {
        "url": sub.get("url", ""),
        "header": sub.get("header", {}) or {},
    }


def update_subscription(url: str, header: Dict[str, Any] | None = None) -> None:
    header = header or {}
    data = _load_yaml(MAIN_CONFIG)
    providers = data.setdefault("proxy-providers", {})
    sub = providers.setdefault("subscription", {})
    if url:
        sub["url"] = url
    if header:
        # normalize headers to list values (mihomo expects list for repeated headers)
        norm: Dict[str, Any] = {}
        for k, v in header.items():
            if isinstance(v, list):
                norm[k] = v
            else:
                norm[k] = [str(v)]
        sub["header"] = norm
    _write_yaml(MAIN_CONFIG, data)
