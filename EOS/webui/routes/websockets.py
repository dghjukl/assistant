from fastapi import APIRouter
from webui.app_runtime import websocket_chat, websocket_admin

router = APIRouter()
router.add_api_websocket_route('/ws', websocket_chat)
router.add_api_websocket_route('/admin/ws', websocket_admin)
