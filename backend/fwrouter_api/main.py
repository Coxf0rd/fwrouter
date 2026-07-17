from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import os
import uvicorn
from fastapi import FastAPI

from fwrouter_api.core.config import get_settings
from fwrouter_api.jobs.extended_handlers import register_extended_handlers
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.bootstrap import bootstrap_backend
from fwrouter_api.services.maintenance_scheduler import (
    start_maintenance_scheduler,
    stop_maintenance_scheduler,
)
from fwrouter_api.services.runtime_prewarm import prime_runtime_read_models_async
from fwrouter_api.services.runtime_convergence_scheduler import (
    start_runtime_convergence_scheduler,
    stop_runtime_convergence_scheduler,
)
from fwrouter_api.services.watchdog import (
    start_watchdog_scheduler,
    stop_watchdog_scheduler,
)
from fwrouter_api.routes.core import router as core_router
from fwrouter_api.routes.jobs import router as jobs_router
from fwrouter_api.routes.logs import router as logs_router
from fwrouter_api.routes.mihomo import router as mihomo_router
from fwrouter_api.routes.modules import router as modules_router
from fwrouter_api.routes.operations import router as operations_router
from fwrouter_api.routes.rules import router as rules_router
from fwrouter_api.routes.runtime import router as runtime_router
from fwrouter_api.routes.selector import router as selector_router
from fwrouter_api.routes.server_ping import router as server_ping_router
from fwrouter_api.routes.servers import router as servers_router
from fwrouter_api.routes.subjects import router as subjects_router
from fwrouter_api.routes.subscription import router as subscription_router
from fwrouter_api.routes.system import router as system_router
from fwrouter_api.routes.system_subjects import router as system_subjects_router
from fwrouter_api.routes.traffic import router as traffic_router
from fwrouter_api.routes.transfer import router as transfer_router
from fwrouter_api.routes.ui import router as ui_router
from fwrouter_api.routes.watchdog import router as watchdog_router
from fwrouter_api.routes.xray import public_router as public_xray_router
from fwrouter_api.routes.xray import router as xray_router


API_PREFIX = "/api/v2"


def _startup_tasks_enabled() -> bool:
    raw = os.getenv("FWROUTER_STARTUP_TASKS_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def create_app(*, enable_startup_tasks: bool | None = None) -> FastAPI:
    settings = get_settings()
    startup_tasks_enabled = _startup_tasks_enabled() if enable_startup_tasks is None else bool(enable_startup_tasks)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if startup_tasks_enabled:
            bootstrap_backend()
            register_extended_handlers(get_default_job_manager())
            start_maintenance_scheduler()
            start_runtime_convergence_scheduler()
            start_watchdog_scheduler()
            prime_runtime_read_models_async(include_global_profiles=False)
        try:
            yield
        finally:
            if startup_tasks_enabled:
                stop_maintenance_scheduler()
                stop_runtime_convergence_scheduler()
                stop_watchdog_scheduler()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=f"{API_PREFIX}/redoc",
        openapi_url=f"{API_PREFIX}/openapi.json",
        lifespan=lifespan,
    )

    app.include_router(system_router, prefix=API_PREFIX)
    app.include_router(system_subjects_router, prefix=API_PREFIX)
    app.include_router(core_router, prefix=API_PREFIX)
    app.include_router(modules_router, prefix=API_PREFIX)
    app.include_router(jobs_router, prefix=API_PREFIX)
    app.include_router(operations_router, prefix=API_PREFIX)
    app.include_router(subjects_router, prefix=API_PREFIX)
    app.include_router(servers_router, prefix=API_PREFIX)
    app.include_router(rules_router, prefix=API_PREFIX)
    app.include_router(subscription_router, prefix=API_PREFIX)
    app.include_router(logs_router, prefix=API_PREFIX)
    app.include_router(runtime_router, prefix=API_PREFIX)
    app.include_router(mihomo_router, prefix=API_PREFIX)
    app.include_router(selector_router, prefix=API_PREFIX)
    app.include_router(watchdog_router, prefix=API_PREFIX)
    app.include_router(server_ping_router, prefix=API_PREFIX)
    app.include_router(traffic_router, prefix=API_PREFIX)
    app.include_router(transfer_router, prefix=API_PREFIX)
    app.include_router(ui_router, prefix=API_PREFIX)
    app.include_router(xray_router, prefix=API_PREFIX)
    app.include_router(public_xray_router)

    return app


app = create_app()


def run() -> None:
    settings = get_settings()

    uvicorn.run(
        "fwrouter_api.main:app",
        host=settings.bind_host,
        port=settings.bind_port,
        reload=False,
        access_log=False,
    )


if __name__ == "__main__":
    run()
