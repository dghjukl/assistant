from fastapi import APIRouter
from webui.app_runtime import (
    get_status_endpoint, get_tools_list, get_memory_recent, get_presence, get_initiative,
    post_chat, post_tts, post_upload, get_vision_settings, post_vision_settings,
    get_identity, get_autonomy, post_autonomy, post_identity_eval, get_relational,
)

router = APIRouter()
router.add_api_route('/api/status', get_status_endpoint, methods=['GET'])
router.add_api_route('/api/tools', get_tools_list, methods=['GET'])
router.add_api_route('/api/memory/recent', get_memory_recent, methods=['GET'])
router.add_api_route('/api/presence', get_presence, methods=['GET'])
router.add_api_route('/api/initiative', get_initiative, methods=['GET'])
router.add_api_route('/api/chat', post_chat, methods=['POST'])
router.add_api_route('/api/tts', post_tts, methods=['POST'])
router.add_api_route('/api/upload', post_upload, methods=['POST'])
router.add_api_route('/api/vision/settings', get_vision_settings, methods=['GET'])
router.add_api_route('/api/vision/settings', post_vision_settings, methods=['POST'])
router.add_api_route('/api/identity', get_identity, methods=['GET'])
router.add_api_route('/api/autonomy', get_autonomy, methods=['GET'])
router.add_api_route('/api/autonomy', post_autonomy, methods=['POST'])
router.add_api_route('/api/identity/eval', post_identity_eval, methods=['POST'])
router.add_api_route('/api/relational', get_relational, methods=['GET'])
