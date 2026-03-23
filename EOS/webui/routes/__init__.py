from .pages import router as pages_router
from .user_api import router as user_api_router
from .admin_api import router as admin_api_router
from .auth import router as auth_router
from .connectors import router as connectors_router
from .diagnostics import router as diagnostics_router
from .websockets import router as websockets_router

__all__ = [
    'pages_router',
    'user_api_router',
    'admin_api_router',
    'auth_router',
    'connectors_router',
    'diagnostics_router',
    'websockets_router',
]
