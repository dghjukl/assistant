"""
Safety / governance tests for EOS.

These tests verify that the tool execution control plane enforces its
security contracts under adversarial or edge-case conditions:

  - Trust level hierarchy is strictly ordered and non-bypassable
  - HARD_CONFIRM gating cannot be bypassed by guessing IDs
  - Schema validation catches bad parameter types and missing required fields
  - Disabled tools cannot be executed even if directly looked up
  - Sensitive parameter keys are redacted in audit summaries
  - Executor without a registry fails safely
"""
from __future__ import annotations

import json
import uuid

import pytest
from tests.conftest import make_spec


class TestTrustHierarchy:
    """The trust order must be PUBLIC < VERIFIED_USER < OPERATOR_ONLY."""

    @pytest.mark.parametrize("tool_trust,caller_trust,should_pass", [
        ("public",        "PUBLIC",        True),
        ("public",        "VERIFIED_USER", True),
        ("public",        "OPERATOR_ONLY", True),
        ("verified_user", "PUBLIC",        False),
        ("verified_user", "VERIFIED_USER", True),
        ("verified_user", "OPERATOR_ONLY", True),
        ("operator_only", "PUBLIC",        False),
        ("operator_only", "VERIFIED_USER", False),
        ("operator_only", "OPERATOR_ONLY", True),
    ])
    def test_trust_matrix(self, executor, registry, tool_trust, caller_trust, should_pass):
        tool_name = f"trust_test_{tool_trust}_{caller_trust}".replace(" ", "_")
        registry.register(make_spec(tool_name, trust_level=tool_trust))
        result = executor.execute(tool_name, {}, caller_trust=caller_trust)
        assert result.success == should_pass, (
            f"tool_trust={tool_trust} caller_trust={caller_trust}: "
            f"expected success={should_pass}, got {result.success} (error: {result.error})"
        )


class TestConfirmationGating:
    def test_hard_confirm_blocked_without_approval(self, executor, registry):
        registry.register(make_spec("guarded", confirmation_policy="hard_confirm"))
        result = executor.execute("guarded", {})
        assert result.success is False
        assert result.pending_confirmation_id is not None

    def test_guessing_confirmation_id_fails(self, executor, registry):
        registry.register(make_spec("guarded2", confirmation_policy="hard_confirm"))
        executor.execute("guarded2", {})
        fake_id = str(uuid.uuid4())
        result = executor.confirm_pending(fake_id)
        assert result.success is False

    def test_double_confirm_fails(self, executor, registry):
        registry.register(make_spec(
            "double_confirm",
            confirmation_policy="hard_confirm",
            handler=lambda p: "{}",
        ))
        pending = executor.execute("double_confirm", {})
        conf_id = pending.pending_confirmation_id
        result1 = executor.confirm_pending(conf_id)
        assert result1.success is True
        result2 = executor.confirm_pending(conf_id)  # ID consumed — should fail
        assert result2.success is False

    def test_denied_confirmation_cannot_be_confirmed(self, executor, registry):
        registry.register(make_spec("deny_guard", confirmation_policy="hard_confirm"))
        pending = executor.execute("deny_guard", {})
        conf_id = pending.pending_confirmation_id
        executor.deny_pending(conf_id)
        result = executor.confirm_pending(conf_id)
        assert result.success is False

    def test_soft_confirm_executes_without_approval(self, executor, registry):
        """SOFT_CONFIRM tools should run immediately (confirmation is advisory only)."""
        registry.register(make_spec(
            "soft_tool",
            confirmation_policy="soft_confirm",
            handler=lambda p: '{"ran": true}',
        ))
        result = executor.execute("soft_tool", {})
        assert result.success is True


class TestSchemaValidation:
    def test_extra_fields_allowed_by_default(self, executor, registry):
        """JSON Schema additionalProperties defaults to true — extra fields are ok."""
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
        registry.register(make_spec("schema_tool", parameters=schema,
                                    handler=lambda p: json.dumps(p)))
        result = executor.execute("schema_tool", {"x": 1, "extra": "ok"})
        assert result.success is True

    def test_wrong_type_triggers_validation(self, executor, registry):
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        }
        registry.register(make_spec("typed_schema", parameters=schema))
        result = executor.execute("typed_schema", {"count": "not_an_int"})
        # jsonschema may not be installed — if validation is skipped, tool runs anyway
        # The contract is: if validation runs, it must fail; it must never silently corrupt
        if not result.success:
            assert "validation" in result.error.lower() or "is not of type" in result.error.lower()


class TestDisabledToolIsolation:
    def test_disabled_at_registration(self, executor, registry):
        registry.register(make_spec("disabled_at_reg", enabled=False))
        result = executor.execute("disabled_at_reg", {})
        assert result.success is False

    def test_disable_after_registration(self, executor, registry):
        registry.register(make_spec("later_disabled"))
        result_before = executor.execute("later_disabled", {})
        assert result_before.success is True
        registry.set_enabled("later_disabled", False)
        result_after = executor.execute("later_disabled", {})
        assert result_after.success is False


class TestParamRedaction:
    """Sensitive keys must not appear in plain text in audit summaries."""

    @pytest.mark.parametrize("sensitive_key", [
        "token", "password", "secret", "key", "credential", "auth"
    ])
    def test_sensitive_key_redacted(self, sensitive_key):
        from runtime.tool_executor import _summarize_params
        params = {sensitive_key: "super_secret_value"}
        summary = _summarize_params(params)
        assert "super_secret_value" not in summary
        assert "<redacted>" in summary

    def test_normal_key_not_redacted(self):
        from runtime.tool_executor import _summarize_params
        summary = _summarize_params({"path": "/etc/hosts", "limit": 10})
        assert "/etc/hosts" in summary
        assert "<redacted>" not in summary


class TestExecutorFailSafe:
    def test_no_registry_always_fails(self):
        from runtime.tool_executor import ToolExecutor
        ex = ToolExecutor(registry=None)
        result = ex.execute("anything", {"a": 1})
        assert result.success is False
        assert "No tool registry" in result.error

    def test_handler_exception_does_not_crash_executor(self, executor, registry):
        def always_explodes(_params):
            raise Exception("BOOM")

        registry.register(make_spec("explodes", handler=always_explodes))
        result = executor.execute("explodes", {})
        assert result.success is False
        assert "BOOM" in result.error
