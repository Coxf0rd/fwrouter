from __future__ import annotations

from threading import Event, Lock, Thread


_PREWARM_LOCK = Lock()
_PREWARM_RUNNING = False
_PREWARM_STOP = Event()
_PREWARM_THREAD: Thread | None = None


def warm_runtime_read_models(*, include_global_profiles: bool = True) -> None:
    from fwrouter_api.services.runtime import get_runtime_summary
    from fwrouter_api.services.system_summary import build_system_summary
    from fwrouter_api.services.ui_state import (
        get_ui_router_summary,
        get_ui_settings_workspace,
        list_ui_clients,
    )

    # Stop between complete read-only projections, never interrupt a writer.
    for projection in (get_runtime_summary, build_system_summary, list_ui_clients,
                       get_ui_router_summary, get_ui_settings_workspace):
        if _PREWARM_STOP.is_set():
            return
        projection()
    if include_global_profiles and not _PREWARM_STOP.is_set():
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
    global _PREWARM_RUNNING, _PREWARM_THREAD
    with _PREWARM_LOCK:
        if _PREWARM_RUNNING or _PREWARM_STOP.is_set():
            return False
        _PREWARM_RUNNING = True
        _PREWARM_THREAD = Thread(
            target=_prewarm_worker,
            kwargs={"include_global_profiles": include_global_profiles},
            name="fwrouter-runtime-prewarm",
            daemon=True,
        )
        _PREWARM_THREAD.start()
    return True


def stop_runtime_read_models_prewarm(*, timeout_seconds: float = 2.0) -> bool:
    """Retain admission ownership until the current read-only projection exits."""
    with _PREWARM_LOCK:
        _PREWARM_STOP.set()
        worker = _PREWARM_THREAD
    if worker is None:
        return True
    worker.join(timeout=max(0.0, timeout_seconds))
    return not worker.is_alive()


def resume_runtime_read_models_prewarm() -> bool:
    """Open admission only at a new application lifespan, after the owner exited."""
    with _PREWARM_LOCK:
        if _PREWARM_THREAD is not None and _PREWARM_THREAD.is_alive():
            return False
        _PREWARM_STOP.clear()
        return True
