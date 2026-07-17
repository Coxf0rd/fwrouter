from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx
import yaml


class SubscriptionRefreshStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True)
class SubscriptionServer:
    """Server parsed from a Mihomo/Clash provider subscription."""

    server_id: str
    server_name: str
    provider_name: str | None = None
    country_code: str | None = None
    region: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubscriptionRefreshResult:
    """Result of provider subscription refresh."""

    status: SubscriptionRefreshStatus
    servers: list[SubscriptionServer] = field(default_factory=list)
    message: str = ""
    error_code: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == SubscriptionRefreshStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status.value,
            "servers": [
                {
                    "server_id": server.server_id,
                    "server_name": server.server_name,
                    "provider_name": server.provider_name,
                    "country_code": server.country_code,
                    "region": server.region,
                    "raw": server.raw,
                }
                for server in self.servers
            ],
            "message": self.message,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "metadata": self.metadata,
        }


class SubscriptionAdapter:
    """Base interface for VPN provider subscription refresh."""

    def refresh(self, url: str) -> SubscriptionRefreshResult:
        raise NotImplementedError


class HttpMihomoSubscriptionAdapter(SubscriptionAdapter):
    """Download and parse Mihomo/Clash YAML subscriptions.

    This adapter only downloads and parses provider data. It does not write
    Mihomo config, does not update SQLite and does not restart containers.
    """

    def __init__(self, *, timeout_seconds: float = 20.0) -> None:
        self.timeout_seconds = timeout_seconds

    def refresh(self, url: str) -> SubscriptionRefreshResult:
        normalized_url = url.strip()

        if not normalized_url:
            return SubscriptionRefreshResult(
                status=SubscriptionRefreshStatus.FAILED,
                message="Subscription URL is empty.",
                error_code="SUBSCRIPTION_URL_EMPTY",
                error_message="SUBSCRIPTION_URL_EMPTY",
            )

        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers={
                    "User-Agent": "FlClashX/1.0.0",
                    "Accept": "text/plain, application/yaml, application/x-yaml, application/json, */*",
                    "x-hwid": "fwrouter-v2-minis",
                    "x-device-os": "Linux",
                    "x-ver-os": "Debian 12",
                    "x-device-model": "FWRouter v2 minis",
                },
            ) as client:
                response = client.get(normalized_url)
                response.raise_for_status()
                body = response.text
        except httpx.HTTPError as exc:
            return SubscriptionRefreshResult(
                status=SubscriptionRefreshStatus.FAILED,
                message="Subscription download failed.",
                error_code="SUBSCRIPTION_DOWNLOAD_FAILED",
                error_message=str(exc),
                metadata={"url": normalized_url},
            )

        return parse_mihomo_subscription_yaml(
            body,
            metadata={
                "url": normalized_url,
                "bytes": len(body.encode("utf-8")),
                "content_type": response.headers.get("content-type"),
            },
        )


def _country_code_from_regional_indicator_emoji(text: str) -> str | None:
    letters: list[str] = []
    for char in text:
        codepoint = ord(char)
        if 0x1F1E6 <= codepoint <= 0x1F1FF:
            letters.append(chr(ord("A") + codepoint - 0x1F1E6))
            if len(letters) == 2:
                return "".join(letters)
        else:
            letters = []
    return None


def parse_mihomo_subscription_yaml(
    text: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> SubscriptionRefreshResult:
    """Parse Mihomo/Clash YAML subscription text."""

    try:
        parsed = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.FAILED,
            message="Subscription YAML parse failed.",
            error_code="SUBSCRIPTION_YAML_INVALID",
            error_message=str(exc),
            metadata=metadata or {},
        )

    if not isinstance(parsed, dict):
        return SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.FAILED,
            message="Subscription payload must be a YAML object.",
            error_code="SUBSCRIPTION_FORMAT_UNSUPPORTED",
            error_message="Top-level YAML value is not an object.",
            metadata=metadata or {},
        )

    proxies = parsed.get("proxies")
    if not isinstance(proxies, list):
        return SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.FAILED,
            message="Subscription does not contain a proxies list.",
            error_code="SUBSCRIPTION_PROXIES_MISSING",
            error_message="Expected top-level 'proxies' list in Mihomo/Clash YAML.",
            metadata={
                **(metadata or {}),
                "top_level_keys": sorted(str(key) for key in parsed.keys()),
            },
        )

    servers: list[SubscriptionServer] = []
    seen_names: set[str] = set()

    # Simple mapping for common countries based on names, can be expanded
    emoji_to_country_code = {
        "🇫🇮": "FI", "🇳🇱": "NL", "🇳🇴": "NO", "🇩🇪": "DE", "🇵🇱": "PL", "🇨🇿": "CZ",
        "🇷🇴": "RO", "🇬🇧": "GB", "🇸🇪": "SE", "🇸🇬": "SG", "🇺🇸": "US", "🇫🇷": "FR",
        "🇯🇵": "JP", "🇨🇦": "CA", "🇦🇺": "AU", "🇧🇷": "BR", "🇮🇳": "IN", "🇮🇩": "ID",
        "🇭🇰": "HK", "🇰🇷": "KR", "🇷🇺": "RU", "🇺🇦": "UA", "🇨🇭": "CH", "🇨🇾": "CY",
        "🇩🇰": "DK", "🇪🇪": "EE", "🇭🇺": "HU", "🇲🇩": "MD", "🇵🇰": "PK", "🇷🇸": "RS",
        "🇿🇦": "ZA", "🇹🇭": "TH", "🇹🇷": "TR", "🇺🇿": "UZ", "🇻🇳": "VN", "🇧🇦": "BA",
        "🇧🇬": "BG", "🇦🇲": "AM", "🇦🇿": "AZ", "🇰🇿": "KZ", "🇱🇻": "LV", "🇱🇹": "LT",
        "🇳🇬": "NG", "🇮🇱": "IL", "🇪🇸": "ES", "🇬🇪": "GE", "🇦🇹": "AT", "🇮🇹": "IT",
    }
    
    country_name_to_code = {
        "finland": "FI", "netherlands": "NL", "norway": "NO", "germany": "DE", "poland": "PL",
        "czech republic": "CZ", "romania": "RO", "united kingdom": "GB", "sweden": "SE",
        "singapore": "SG", "usa": "US", "france": "FR", "japan": "JP", "canada": "CA",
        "australia": "AU", "brazil": "BR", "india": "IN", "indonesia": "ID", "hong kong": "HK",
        "south korea": "KR", "russia": "RU", "ukraine": "UA", "switzerland": "CH", "cyprus": "CY",
        "denmark": "DK", "estonia": "EE", "hungary": "HU", "moldova": "MD", "pakistan": "PK",
        "serbia": "RS", "south africa": "ZA", "thailand": "TH", "turkey": "TR", "uzbekistan": "UZ",
        "vietnam": "VN", "bosnia and herzegovina": "BA", "bulgaria": "BG", "armenia": "AM",
        "azerbaijan": "AZ", "kazakhstan": "KZ", "latvia": "LV", "lithuania": "LT", "nigeria": "NG",
        "israel": "IL", "spain": "ES", "georgia": "GE", "austria": "AT", "italy": "IT",
    }

    for index, proxy in enumerate(proxies, start=1):
        if not isinstance(proxy, dict):
            continue

        name = str(proxy.get("name") or "").strip()
        if not name:
            continue

        if name in seen_names:
            continue

        seen_names.add(name)
        
        extracted_country_code = _country_code_from_regional_indicator_emoji(name)

        # 1. Try to extract from known emoji fallbacks.
        if not extracted_country_code:
            for emoji, code in emoji_to_country_code.items():
                if emoji in name:
                    extracted_country_code = code
                    break
        
        # 2. If not found by emoji, try to extract from country name
        if not extracted_country_code:
            lower_name = name.lower()
            for country_name, code in country_name_to_code.items():
                if country_name in lower_name:
                    extracted_country_code = code
                    break
        
        servers.append(
            SubscriptionServer(
                server_id=name,
                server_name=name,
                provider_name=str(proxy.get("provider") or "subscription"),
                country_code=extracted_country_code,
                region=None,
                raw=proxy,
            )
        )

    if not servers:
        return SubscriptionRefreshResult(
            status=SubscriptionRefreshStatus.FAILED,
            message="Subscription contains no usable proxies.",
            error_code="SUBSCRIPTION_SERVERS_EMPTY",
            error_message="No proxy entries with a non-empty name were found.",
            metadata={
                **(metadata or {}),
                "proxies_count": len(proxies),
            },
        )

    return SubscriptionRefreshResult(
        status=SubscriptionRefreshStatus.SUCCESS,
        servers=servers,
        message="Subscription parsed successfully.",
        metadata={
            **(metadata or {}),
            "proxies_count": len(proxies),
            "servers_count": len(servers),
        },
    )


DEFAULT_SUBSCRIPTION_ADAPTER = HttpMihomoSubscriptionAdapter()
