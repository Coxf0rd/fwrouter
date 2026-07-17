from __future__ import annotations

from threading import Lock, Thread


_PREWARM_LOCK = Lock()
_PREWARM_RUNNING = False


def warm_runtime_read_models(*, include_global_profiles: bool = True) -> None:
    from fwrouter_api.services.runtime import get_runtime_summary
    from fwrouter_api.services.system_summary import build_system_summary
    from fwrouter_api.services.ui_state import (
        get_ui_router_summary,
        get_ui_settings_workspace,
        list_ui_clients,
    )

    get_runtime_summary()
    build_system_summary()
    list_ui_clients()
    get_ui_router_summary()
    get_ui_settings_workspace()
    if include_global_profiles:
        from fwrouter_api.services.global_mode_profiles import compile_all_global_mode_profiles

        compile_all_global_mode_profiles()


def _prewarm_worker(*, include_global_profiles: bool) -> None:
    global _PREWARM_RUNNING
    try:
        warm_runtime_read_models(include_global_profiles=include_global_profiles)
    except Exception:
        pass
    finally:
        with _PREWARM_LOCK:
            _PREWARM_RUNNING = False


def prime_runtime_read_models_async(*, include_global_profiles: bool = True) -> bool:
    global _PREWARM_RUNNING
    with _PREWARM_LOCK:
        if _PREWARM_RUNNING:
            return False
        _PREWARM_RUNNING = True

    Thread(
        target=_prewarm_worker,
        kwargs={"include_global_profiles": include_global_profiles},
        name="fwrouter-runtime-prewarm",
        daemon=True,
    ).start()
    return True
