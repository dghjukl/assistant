from fastapi import APIRouter
from webui.app_runtime import auth_verify, api_lan_pair, api_lan_status

router = APIRouter()
router.add_api_route('/api/auth/verify', auth_verify, methods=['GET'])
router.add_api_route('/api/auth/lan/pair', api_lan_pair, methods=['POST'])
router.add_api_route('/api/auth/lan/status', api_lan_status, methods=['GET'])
