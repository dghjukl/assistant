from fastapi import APIRouter
from webui.app_runtime import (
    admin_runtime_diagnostics, admin_entity_state_diagnostic, admin_environment_model_diagnostic,
    admin_tool_registry, admin_shadow_databases, admin_memory_health, admin_memory_maintenance,
    admin_memory_maintenance_last, admin_degradation_status, admin_connectivity,
    admin_system_sensors, admin_capabilities, admin_crash_recovery, admin_backend_health,
    admin_idle_cognition_status, admin_identity_continuity, admin_identity_revisions,
    admin_force_idle_cognition, admin_entity_lifecycle, admin_session_continuity,
)

router = APIRouter()
router.add_api_route('/admin/runtime-diagnostics', admin_runtime_diagnostics, methods=['GET'])
router.add_api_route('/admin/diagnostic/entity-state', admin_entity_state_diagnostic, methods=['GET'])
router.add_api_route('/admin/diagnostic/environment-model', admin_environment_model_diagnostic, methods=['GET'])
router.add_api_route('/admin/tool-registry-diagnostics', admin_tool_registry, methods=['GET'])
router.add_api_route('/admin/shadow-databases', admin_shadow_databases, methods=['GET'])
router.add_api_route('/admin/memory/health', admin_memory_health, methods=['GET'])
router.add_api_route('/admin/memory/maintenance', admin_memory_maintenance, methods=['POST'])
router.add_api_route('/admin/memory/maintenance/last', admin_memory_maintenance_last, methods=['GET'])
router.add_api_route('/admin/degradation/status', admin_degradation_status, methods=['GET'])
router.add_api_route('/admin/diagnostic/connectivity', admin_connectivity, methods=['GET'])
router.add_api_route('/admin/system/sensors', admin_system_sensors, methods=['GET'])
router.add_api_route('/admin/system/capabilities', admin_capabilities, methods=['GET'])
router.add_api_route('/admin/system/crash-recovery', admin_crash_recovery, methods=['GET'])
router.add_api_route('/admin/system/backend-health', admin_backend_health, methods=['GET'])
router.add_api_route('/admin/system/idle-cognition', admin_idle_cognition_status, methods=['GET'])
router.add_api_route('/admin/identity/continuity', admin_identity_continuity, methods=['GET'])
router.add_api_route('/admin/identity/continuity/revisions', admin_identity_revisions, methods=['GET'])
router.add_api_route('/admin/system/idle-cognition/force', admin_force_idle_cognition, methods=['POST'])
router.add_api_route('/admin/system/lifecycle', admin_entity_lifecycle, methods=['GET'])
router.add_api_route('/admin/system/session-continuity', admin_session_continuity, methods=['GET'])
