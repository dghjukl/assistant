from fastapi import APIRouter
from webui.app_runtime import (
    google_status, google_authorize, google_oauth_callback, google_revoke, google_account,
    google_calendar_today, google_calendar_upcoming, google_gmail_inbox, google_drive_recent,
    google_drive_search, discord_status, discord_connect, discord_disconnect,
)

router = APIRouter()
router.add_api_route('/api/google_workspace/status', google_status, methods=['GET'])
router.add_api_route('/api/google_workspace/authorize', google_authorize, methods=['GET'])
router.add_api_route('/api/google_workspace/callback', google_oauth_callback, methods=['GET'])
router.add_api_route('/api/google_workspace/revoke', google_revoke, methods=['POST'])
router.add_api_route('/api/google_workspace/account', google_account, methods=['GET'])
router.add_api_route('/api/google_workspace/calendar/today', google_calendar_today, methods=['GET'])
router.add_api_route('/api/google_workspace/calendar/upcoming', google_calendar_upcoming, methods=['GET'])
router.add_api_route('/api/google_workspace/gmail/inbox', google_gmail_inbox, methods=['GET'])
router.add_api_route('/api/google_workspace/drive/recent', google_drive_recent, methods=['GET'])
router.add_api_route('/api/google_workspace/drive/search', google_drive_search, methods=['GET'])
router.add_api_route('/api/discord/status', discord_status, methods=['GET'])
router.add_api_route('/api/discord/connect', discord_connect, methods=['POST'])
router.add_api_route('/api/discord/disconnect', discord_disconnect, methods=['POST'])
