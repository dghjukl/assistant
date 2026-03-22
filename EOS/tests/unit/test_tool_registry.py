"""Unit tests for runtime.tool_registry.ToolRegistry."""
from __future__ import annotations

import pytest
from tests.conftest import make_spec


class TestToolRegistryRegistration:
    def test_register_and_get(self, registry):
        spec = make_spec("alpha")
        registry.register(spec)
        assert registry.get("alpha") is spec

    def test_duplicate_raises(self, registry):
        registry.register(make_spec("dup"))
        with pytest.raises(ValueError, match="already registered"):
            registry.register(make_spec("dup"))

    def test_empty_name_raises(self, registry):
        with pytest.raises(ValueError):
            from runtime.tool_registry import ToolSpec
            ToolSpec(
                name="",
                description="bad",
                pack="p",
                tags=[],
                parameters={},
                handler=lambda p: "",
                risk_level="read_only",
                trust_level="public",
                confirmation_policy="none",
            )

    def test_invalid_risk_level_raises(self, registry):
        with pytest.raises(ValueError, match="risk_level"):
            make_spec("bad_risk", risk_level="ultra_dangerous")

    def test_invalid_trust_level_raises(self, registry):
        with pytest.raises(ValueError, match="trust_level"):
            make_spec("bad_trust", trust_level="root")

    def test_invalid_confirmation_policy_raises(self, registry):
        with pytest.raises(ValueError, match="confirmation_policy"):
            make_spec("bad_policy", confirmation_policy="maybe")


class TestToolRegistryQueries:
    def test_all_tools_returns_all(self, registry):
        registry.register(make_spec("t1"))
        registry.register(make_spec("t2", enabled=False))
        all_tools = registry.all_tools()
        assert len(all_tools) == 2

    def test_all_enabled_excludes_disabled(self, registry):
        registry.register(make_spec("on"))
        registry.register(make_spec("off", enabled=False))
        enabled = registry.all_enabled()
        assert len(enabled) == 1
        assert enabled[0].name == "on"

    def test_get_disabled_returns_none(self, registry):
        registry.register(make_spec("disabled_tool", enabled=False))
        assert registry.get("disabled_tool") is None

    def test_get_unknown_returns_none(self, registry):
        assert registry.get("nonexistent") is None

    def test_set_enabled_toggles(self, registry):
        registry.register(make_spec("toggleable"))
        assert registry.get("toggleable") is not None
        registry.set_enabled("toggleable", False)
        assert registry.get("toggleable") is None
        registry.set_enabled("toggleable", True)
        assert registry.get("toggleable") is not None

    def test_set_enabled_unknown_raises(self, registry):
        with pytest.raises(KeyError):
            registry.set_enabled("ghost", True)

    def test_by_pack(self, registry):
        from runtime.tool_registry import ToolSpec
        spec = ToolSpec(
            name="pack_tool", description="x", pack="my_pack", tags=[],
            parameters={}, handler=lambda p: "",
            risk_level="read_only", trust_level="public", confirmation_policy="none",
        )
        registry.register(spec)
        registry.register(make_spec("other"))
        assert len(registry.by_pack("my_pack")) == 1

    def test_by_tag(self, registry):
        from runtime.tool_registry import ToolSpec
        spec = ToolSpec(
            name="tagged", description="x", pack="p", tags=["special"],
            parameters={}, handler=lambda p: "",
            risk_level="read_only", trust_level="public", confirmation_policy="none",
        )
        registry.register(spec)
        registry.register(make_spec("untagged"))
        assert len(registry.by_tag("special")) == 1

    def test_summary_counts(self, registry):
        registry.register(make_spec("s1"))
        registry.register(make_spec("s2", enabled=False))
        summary = registry.summary()
        assert summary["total_tools"] == 2
        assert summary["enabled_tools"] == 1
        assert summary["disabled_tools"] == 1


class TestToolRegistryAudit:
    def test_record_execution_only_for_commit_risk(self, registry):
        registry.register(make_spec("read", risk_level="read_only"))
        registry.register(make_spec("commit", risk_level="reversible_commit"))
        # read_only — should NOT be audited
        audit_id = registry.record_execution("read", success=True, params_summary="")
        assert audit_id is None
        # reversible_commit — should be audited
        audit_id2 = registry.record_execution("commit", success=True, params_summary="x=1")
        assert audit_id2 is not None

    def test_recent_audit_capped(self, registry):
        registry.register(make_spec("commit_tool", risk_level="irreversible_commit"))
        for i in range(5):
            registry.record_execution("commit_tool", success=True, params_summary=str(i))
        entries = registry.recent_audit(3)
        assert len(entries) == 3
