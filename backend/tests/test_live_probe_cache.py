from __future__ import annotations

from fwrouter_api.services.live_probe_cache import clear_live_probe_cache, get_live_probe_cache


def test_live_probe_cache_reuses_value_within_ttl() -> None:
    clear_live_probe_cache()
    calls: list[int] = []

    def _load() -> dict[str, int]:
        calls.append(1)
        return {"calls": len(calls)}

    first = get_live_probe_cache("probe", ttl_seconds=10.0, loader=_load)
    second = get_live_probe_cache("probe", ttl_seconds=10.0, loader=_load)

    assert first == {"calls": 1}
    assert second == {"calls": 1}
    assert len(calls) == 1


def test_live_probe_cache_starts_ttl_after_loader_finishes(monkeypatch) -> None:
    clear_live_probe_cache()
    calls: list[int] = []
    clock = [100.0]

    def _monotonic() -> float:
        return clock[0]

    def _load() -> dict[str, int]:
        calls.append(1)
        clock[0] += 3.0
        return {"calls": len(calls)}

    monkeypatch.setattr("fwrouter_api.services.live_probe_cache.monotonic", _monotonic)

    first = get_live_probe_cache("slow-probe", ttl_seconds=2.0, loader=_load)
    second = get_live_probe_cache("slow-probe", ttl_seconds=2.0, loader=_load)

    assert first == {"calls": 1}
    assert second == {"calls": 1}
    assert len(calls) == 1
