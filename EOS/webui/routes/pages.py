from fastapi import APIRouter
from webui.app_runtime import get_index, get_workspace, get_admin, get_docs, get_docs_content, favicon

router = APIRouter()
router.add_api_route('/', get_index, methods=['GET'])
router.add_api_route('/workspace', get_workspace, methods=['GET'])
router.add_api_route('/admin', get_admin, methods=['GET'])
router.add_api_route('/docs', get_docs, methods=['GET'])
router.add_api_route('/docs/content/{page}', get_docs_content, methods=['GET'])
router.add_api_route('/favicon.ico', favicon, methods=['GET'])
