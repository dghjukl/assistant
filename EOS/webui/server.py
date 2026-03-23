from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from core.auth import AdminAuthMiddleware
from core.access_control import AccessControlMiddleware
from webui.app_state import app_state
from webui.routes import (
    admin_api_router,
    auth_router,
    connectors_router,
    diagnostics_router,
    pages_router,
    user_api_router,
    websockets_router,
)
from webui.startup import on_shutdown, on_startup

startup_event = on_startup
shutdown_event = on_shutdown


def create_app(*, config_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title='EOS WebUI', version='1.0', docs_url=None, redoc_url=None)
    app.state.eos = app_state
    app.state.config_path = str(config_path) if config_path is not None else None
    # Starlette runs middleware in reverse registration order, so add
    # AccessControlMiddleware last to classify origin first, then enforce
    # admin token auth with the resolved origin tier.
    app.add_middleware(AdminAuthMiddleware)
    app.add_middleware(AccessControlMiddleware)

    webui_dir = Path(__file__).parent
    static_dir = webui_dir / 'docs'
    if static_dir.is_dir():
        app.mount('/static/docs', StaticFiles(directory=static_dir), name='static-docs')

    for router in (
        pages_router,
        user_api_router,
        admin_api_router,
        auth_router,
        connectors_router,
        diagnostics_router,
        websockets_router,
    ):
        app.include_router(router)

    async def _startup() -> None:
        await startup_event(app)

    if hasattr(app, 'add_event_handler'):
        app.add_event_handler('startup', _startup)
        app.add_event_handler('shutdown', shutdown_event)
    else:  # FastAPI/Starlette compatibility fallback
        app.router.on_startup.append(_startup)
        app.router.on_shutdown.append(shutdown_event)
    return app


app = create_app()


_STATE_EXPORTS = {
    "_topology": "topology",
    "_cfg": "cfg",
    "_tracer": "tracer",
    "_bus": "bus",
    "_reflection_pipeline": "reflection_pipeline",
    "_initiative_engine": "initiative_engine",
    "_investigation_engine": "investigation_engine",
    "_sensor_poller": "sensor_poller",
    "_crash_recovery": "crash_recovery",
    "_capability_registry": "capability_registry",
    "_backend_probe": "backend_probe",
    "_idle_cognition": "idle_cognition",
    "_identity_continuity": "identity_continuity",
    "_entity_lifecycle": "entity_lifecycle",
    "_session_continuity": "session_continuity",
    "_goal_store": "goal_store",
    "_current_focus_service": "current_focus_service",
    "_workspace_service": "workspace_service",
    "_worldview_service": "worldview_service",
    "_entity_state_service": "entity_state_service",
    "_backup_service": "backup_service",
    "_computer_use_service": "computer_use_service",
    "_overnight_cycle_service": "overnight_cycle_service",
    "_runtime_discovery": "runtime_discovery",
    "_tool_states": "tool_states",
    "_perm_allowlist": "perm_allowlist",
    "_toolpack_states": "toolpack_states",
    "_tool_registry": "tool_registry",
    "_vision_sessions": "vision_sessions",
    "_log_ring": "log_ring",
}


def __getattr__(name: str):
    attr = _STATE_EXPORTS.get(name)
    if attr is not None:
        return getattr(app_state, attr)
    raise AttributeError(name)
