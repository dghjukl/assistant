from fastapi import APIRouter
from webui.app_runtime import (
    admin_get_status, admin_servers_status, admin_all_logs, admin_server_logs,
    admin_get_tools, admin_tools_audit, admin_enable_tool, admin_disable_tool,
    admin_get_permissions, admin_allow_permission, admin_deny_permission,
    admin_get_allowlist, admin_allowlist_add, admin_allowlist_remove,
    admin_get_toolpacks, admin_enable_toolpack, admin_disable_toolpack,
    admin_cu_state, admin_cu_set_mode, admin_cu_halt, admin_cu_shortcuts,
    admin_cu_policies, admin_cu_reload, admin_cu_confirm, admin_cu_deny,
    admin_cognition_turns, admin_cognition_turn_detail, admin_cognition_memory,
    admin_cognition_reflection, admin_cognition_state, admin_cognition_summary,
    admin_get_config, admin_subsystems, admin_export, admin_latency, admin_storage,
    admin_autonomy_status, admin_autonomy_status_update,
    admin_free_mode, admin_free_mode_activate, admin_free_mode_deactivate,
    admin_get_capabilities, admin_set_capability,
    admin_initiative_status, admin_initiative_queue, admin_initiative_trigger,
    admin_initiative_feedback, admin_initiative_execute, admin_initiative_clear,
    admin_investigation_list, admin_investigation_create, admin_investigation_get,
    admin_investigation_run_pass, admin_investigation_resolve, admin_investigation_reopen,
    admin_investigation_delete, admin_investigation_diagnostics,
    admin_force_tool, admin_force_retrieval,
    admin_audit_actions, admin_audit_tools, admin_audit_summary,
    admin_secrets_list, admin_secrets_set, admin_secrets_delete,
    admin_tools_pending, admin_tool_confirm, admin_tool_deny,
    admin_goals_list, admin_goals_create, admin_goals_complete, admin_goals_abandon,
    admin_workspace, admin_workspace_scan,
    admin_overnight_status,
    admin_worldview_status, admin_worldview_refresh, admin_worldview_extract,
    admin_backup_list, admin_backup_create, admin_backup_get, admin_backup_restore,
    admin_integrity_check,
    admin_access_tiers_list, admin_access_tier_update,
    admin_lan_generate_pairing_code, admin_lan_sessions_list, admin_lan_session_revoke,
)

router = APIRouter()
router.add_api_route('/admin/status', admin_get_status, methods=['GET'])
router.add_api_route('/admin/servers/status', admin_servers_status, methods=['GET'])
router.add_api_route('/admin/logs', admin_all_logs, methods=['GET'])
router.add_api_route('/admin/servers/{server_key}/logs', admin_server_logs, methods=['GET'])
router.add_api_route('/admin/tools', admin_get_tools, methods=['GET'])
router.add_api_route('/admin/tools/audit', admin_tools_audit, methods=['GET'])
router.add_api_route('/admin/tools/{tool_name}/enable', admin_enable_tool, methods=['POST'])
router.add_api_route('/admin/tools/{tool_name}/disable', admin_disable_tool, methods=['POST'])
router.add_api_route('/admin/permissions', admin_get_permissions, methods=['GET'])
router.add_api_route('/admin/permissions/{perm_class}/allow', admin_allow_permission, methods=['POST'])
router.add_api_route('/admin/permissions/{perm_class}/deny', admin_deny_permission, methods=['POST'])
router.add_api_route('/admin/config/allowlist', admin_get_allowlist, methods=['GET'])
router.add_api_route('/admin/config/allowlist/add', admin_allowlist_add, methods=['POST'])
router.add_api_route('/admin/config/allowlist/remove', admin_allowlist_remove, methods=['POST'])
router.add_api_route('/admin/toolpacks', admin_get_toolpacks, methods=['GET'])
router.add_api_route('/admin/toolpacks/{pack_name}/enable', admin_enable_toolpack, methods=['POST'])
router.add_api_route('/admin/toolpacks/{pack_name}/disable', admin_disable_toolpack, methods=['POST'])
router.add_api_route('/admin/computer_use/state', admin_cu_state, methods=['GET'])
router.add_api_route('/admin/computer_use/mode', admin_cu_set_mode, methods=['POST'])
router.add_api_route('/admin/computer_use/halt', admin_cu_halt, methods=['POST'])
router.add_api_route('/admin/computer_use/shortcuts', admin_cu_shortcuts, methods=['GET'])
router.add_api_route('/admin/computer_use/policies', admin_cu_policies, methods=['GET'])
router.add_api_route('/admin/computer_use/reload', admin_cu_reload, methods=['POST'])
router.add_api_route('/admin/computer_use/confirm/{confirmation_id}', admin_cu_confirm, methods=['POST'])
router.add_api_route('/admin/computer_use/deny/{confirmation_id}', admin_cu_deny, methods=['POST'])
router.add_api_route('/admin/cognition/turns', admin_cognition_turns, methods=['GET'])
router.add_api_route('/admin/cognition/turns/{turn_id}', admin_cognition_turn_detail, methods=['GET'])
router.add_api_route('/admin/cognition/memory', admin_cognition_memory, methods=['GET'])
router.add_api_route('/admin/cognition/reflection', admin_cognition_reflection, methods=['GET'])
router.add_api_route('/admin/cognition/state', admin_cognition_state, methods=['GET'])
router.add_api_route('/admin/cognition/summary', admin_cognition_summary, methods=['GET'])
router.add_api_route('/admin/config', admin_get_config, methods=['GET'])
router.add_api_route('/admin/subsystems', admin_subsystems, methods=['GET'])
router.add_api_route('/admin/export', admin_export, methods=['GET'])
router.add_api_route('/admin/latency', admin_latency, methods=['GET'])
router.add_api_route('/admin/storage', admin_storage, methods=['GET'])
router.add_api_route('/admin/autonomy/status', admin_autonomy_status, methods=['GET'])
router.add_api_route('/admin/autonomy/status', admin_autonomy_status_update, methods=['POST'])
router.add_api_route('/admin/free-mode', admin_free_mode, methods=['GET'])
router.add_api_route('/admin/free-mode/activate', admin_free_mode_activate, methods=['POST'])
router.add_api_route('/admin/free-mode/deactivate', admin_free_mode_deactivate, methods=['POST'])
router.add_api_route('/admin/capabilities', admin_get_capabilities, methods=['GET'])
router.add_api_route('/admin/capabilities', admin_set_capability, methods=['POST'])
router.add_api_route('/admin/initiative/status', admin_initiative_status, methods=['GET'])
router.add_api_route('/admin/initiative/queue', admin_initiative_queue, methods=['GET'])
router.add_api_route('/admin/initiative/trigger', admin_initiative_trigger, methods=['POST'])
router.add_api_route('/admin/initiative/feedback', admin_initiative_feedback, methods=['POST'])
router.add_api_route('/admin/initiative/execute', admin_initiative_execute, methods=['POST'])
router.add_api_route('/admin/initiative/clear', admin_initiative_clear, methods=['POST'])
router.add_api_route('/admin/investigation/list', admin_investigation_list, methods=['GET'])
router.add_api_route('/admin/investigation/create', admin_investigation_create, methods=['POST'])
router.add_api_route('/admin/investigation/{investigation_id}', admin_investigation_get, methods=['GET'])
router.add_api_route('/admin/investigation/{investigation_id}/run-pass', admin_investigation_run_pass, methods=['POST'])
router.add_api_route('/admin/investigation/{investigation_id}/resolve', admin_investigation_resolve, methods=['POST'])
router.add_api_route('/admin/investigation/{investigation_id}/reopen', admin_investigation_reopen, methods=['POST'])
router.add_api_route('/admin/investigation/{investigation_id}', admin_investigation_delete, methods=['DELETE'])
router.add_api_route('/admin/investigation/diagnostics', admin_investigation_diagnostics, methods=['GET'])
router.add_api_route('/admin/diagnostic/force-tool', admin_force_tool, methods=['POST'])
router.add_api_route('/admin/diagnostic/force-retrieval', admin_force_retrieval, methods=['POST'])
router.add_api_route('/admin/audit/actions', admin_audit_actions, methods=['GET'])
router.add_api_route('/admin/audit/tools', admin_audit_tools, methods=['GET'])
router.add_api_route('/admin/audit/summary', admin_audit_summary, methods=['GET'])
router.add_api_route('/admin/secrets', admin_secrets_list, methods=['GET'])
router.add_api_route('/admin/secrets/{key}', admin_secrets_set, methods=['POST'])
router.add_api_route('/admin/secrets/{key}', admin_secrets_delete, methods=['DELETE'])
router.add_api_route('/admin/tools/pending', admin_tools_pending, methods=['GET'])
router.add_api_route('/admin/tools/confirm/{confirmation_id}', admin_tool_confirm, methods=['POST'])
router.add_api_route('/admin/tools/deny/{confirmation_id}', admin_tool_deny, methods=['POST'])
router.add_api_route('/admin/entity/goals', admin_goals_list, methods=['GET'])
router.add_api_route('/admin/entity/goals', admin_goals_create, methods=['POST'])
router.add_api_route('/admin/entity/goals/{goal_id}/complete', admin_goals_complete, methods=['POST'])
router.add_api_route('/admin/entity/goals/{goal_id}/abandon', admin_goals_abandon, methods=['POST'])
router.add_api_route('/admin/system/workspace', admin_workspace, methods=['GET'])
router.add_api_route('/admin/system/workspace/scan-context', admin_workspace_scan, methods=['POST'])
router.add_api_route('/admin/system/overnight', admin_overnight_status, methods=['GET'])
router.add_api_route('/admin/system/worldview', admin_worldview_status, methods=['GET'])
router.add_api_route('/admin/system/worldview/refresh', admin_worldview_refresh, methods=['POST'])
router.add_api_route('/admin/system/worldview/extract', admin_worldview_extract, methods=['POST'])
router.add_api_route('/admin/system/backup', admin_backup_list, methods=['GET'])
router.add_api_route('/admin/system/backup', admin_backup_create, methods=['POST'])
router.add_api_route('/admin/system/backup/{backup_id}', admin_backup_get, methods=['GET'])
router.add_api_route('/admin/system/backup/{backup_id}/restore', admin_backup_restore, methods=['POST'])
router.add_api_route('/admin/system/integrity', admin_integrity_check, methods=['GET'])
router.add_api_route('/admin/access-tiers', admin_access_tiers_list, methods=['GET'])
router.add_api_route('/admin/access-tiers/{tier}', admin_access_tier_update, methods=['PATCH'])
router.add_api_route('/admin/access-tiers/lan/pairing-code', admin_lan_generate_pairing_code, methods=['POST'])
router.add_api_route('/admin/access-tiers/lan/sessions', admin_lan_sessions_list, methods=['GET'])
router.add_api_route('/admin/access-tiers/lan/sessions/{token_prefix}', admin_lan_session_revoke, methods=['DELETE'])
