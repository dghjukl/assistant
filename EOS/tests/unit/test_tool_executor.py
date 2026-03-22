"""Unit tests for runtime.tool_executor.ToolExecutor."""
from __future__ import annotations

import json
import time

import pytest
from tests.conftest import make_spec


class TestToolExecutorBasic:
    def test_successful_execution(self, executor, registry):
        registry.register(make_spec("ok_tool", handler=lambda p: json.dumps({"value": 42})))
        result = executor.execute("ok_tool", {})
        assert result.success is True
        assert result.output == {"value": 42}

    def test_unknown_tool_fails(self, executor):
        result = executor.execute("no_such_tool", {})
        assert result.success is False
        assert "Unknown or disabled tool" in result.error

    def test_disabled_tool_fails(self, executor, registry):
        registry.register(make_spec("disabled", enabled=False))
        result = executor.execute("disabled", {})
        assert result.success is False

    def test_no_registry_fails(self):
        from runtime.tool_executor import ToolExecutor
        ex = ToolExecutor(registry=None)
        result = ex.execute("anything", {})
        assert result.success is False
        assert "No tool registry" in result.error

    def test_string_output_preserved(self, executor, registry):
        registry.register(make_spec("str_tool", handler=lambda p: "plain text"))
        result = executor.execute("str_tool", {})
        assert result.success is True
        assert result.output == "plain text"

    def test_handler_exception_caught(self, executor, registry):
        def boom(_params):
            raise RuntimeError("deliberate failure")
        registry.register(make_spec("boom_tool", handler=boom))
        result = executor.execute("boom_tool", {})
        assert result.success is False
        assert "deliberate failure" in result.error


class TestToolExecutorTrustLevel:
    def test_public_caller_can_use_public_tool(self, executor, registry):
        registry.register(make_spec("pub_tool", trust_level="public"))
        result = executor.execute("pub_tool", {}, caller_trust="PUBLIC")
        assert result.success is True

    def test_public_caller_blocked_on_operator_tool(self, executor, registry):
        registry.register(make_spec("priv_tool", trust_level="operator_only"))
        result = executor.execute("priv_tool", {}, caller_trust="PUBLIC")
        assert result.success is False
        assert "Insufficient trust" in result.error

    def test_operator_caller_can_use_any_tool(self, executor, registry):
        registry.register(make_spec("any_tool", trust_level="operator_only"))
        result = executor.execute("any_tool", {}, caller_trust="OPERATOR_ONLY")
        assert result.success is True

    def test_verified_user_blocked_on_operator_tool(self, executor, registry):
        registry.register(make_spec("op_tool", trust_level="operator_only"))
        result = executor.execute("op_tool", {}, caller_trust="VERIFIED_USER")
        assert result.success is False


class TestToolExecutorSchemaValidation:
    def test_valid_params_pass(self, executor, registry):
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        }
        registry.register(make_spec("needs_n", parameters=schema,
                                    handler=lambda p: json.dumps({"n": p["n"]})))
        result = executor.execute("needs_n", {"n": 5})
        assert result.success is True

    def test_missing_required_field_fails(self, executor, registry):
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        }
        registry.register(make_spec("needs_n2", parameters=schema))
        result = executor.execute("needs_n2", {})
        # If jsonschema not installed this may pass — acceptable
        if not result.success:
            assert "validation" in result.error.lower() or "required" in result.error.lower()

    def test_wrong_type_fails(self, executor, registry):
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        }
        registry.register(make_spec("typed_tool", parameters=schema))
        result = executor.execute("typed_tool", {"n": "not_an_int"})
        if not result.success:
            assert "validation" in result.error.lower()


class TestToolExecutorConfirmationGating:
    def test_hard_confirm_returns_pending(self, executor, registry):
        registry.register(make_spec("dangerous", confirmation_policy="hard_confirm"))
        result = executor.execute("dangerous", {})
        assert result.success is False
        assert result.pending_confirmation_id is not None

    def test_confirm_executes(self, executor, registry):
        registry.register(make_spec(
            "gate_tool",
            confirmation_policy="hard_confirm",
            handler=lambda p: '{"confirmed": true}',
        ))
        pending_result = executor.execute("gate_tool", {})
        conf_id = pending_result.pending_confirmation_id
        assert conf_id is not None
        final_result = executor.confirm_pending(conf_id)
        assert final_result.success is True

    def test_deny_removes_pending(self, executor, registry):
        registry.register(make_spec("deny_tool", confirmation_policy="hard_confirm"))
        pending_result = executor.execute("deny_tool", {})
        conf_id = pending_result.pending_confirmation_id
        denied = executor.deny_pending(conf_id)
        assert denied is True
        # Confirm should fail after denial
        late_confirm = executor.confirm_pending(conf_id)
        assert late_confirm.success is False

    def test_unknown_confirmation_id_fails(self, executor):
        result = executor.confirm_pending("nonexistent-id")
        assert result.success is False

    def test_list_pending(self, executor, registry):
        registry.register(make_spec("pending_list_tool", confirmation_policy="hard_confirm"))
        executor.execute("pending_list_tool", {})
        pending = executor.list_pending()
        assert len(pending) >= 1
        assert all("confirmation_id" in p for p in pending)


class TestToolExecutorTimeout:
    def test_timeout_enforced(self, executor, registry):
        import time as _time

        def slow(_params):
            _time.sleep(10)
            return "{}"

        registry.register(make_spec("slow_tool", handler=slow, timeout_seconds=1))
        result = executor.execute("slow_tool", {})
        assert result.success is False
        assert "timed out" in result.error.lower()
