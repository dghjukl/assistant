"""Unit tests for core.audit.AuditStore."""
from __future__ import annotations

import time

import pytest


class TestAuditStoreAdminActions:
    def test_record_and_query_admin_action(self, audit_store):
        action_id = audit_store.record_admin_action(
            "tool_toggle", target="read_file", details={"enabled": True}
        )
        assert action_id

        rows = audit_store.query_admin_actions(action_type="tool_toggle")
        assert len(rows) == 1
        assert rows[0]["target"] == "read_file"
        assert rows[0]["details"] == {"enabled": True}

    def test_query_filters_by_type(self, audit_store):
        audit_store.record_admin_action("type_a")
        audit_store.record_admin_action("type_b")
        rows = audit_store.query_admin_actions(action_type="type_a")
        assert all(r["action_type"] == "type_a" for r in rows)

    def test_query_filters_by_actor(self, audit_store):
        audit_store.record_admin_action("act", actor="alice")
        audit_store.record_admin_action("act", actor="bob")
        rows = audit_store.query_admin_actions(actor="alice")
        assert len(rows) == 1
        assert rows[0]["actor"] == "alice"

    def test_query_since_until(self, audit_store):
        t0 = time.time()
        audit_store.record_admin_action("old_act")
        time.sleep(0.01)
        t1 = time.time()
        audit_store.record_admin_action("new_act")

        rows_new = audit_store.query_admin_actions(since=t1)
        assert len(rows_new) == 1
        assert rows_new[0]["action_type"] == "new_act"

        rows_old = audit_store.query_admin_actions(until=t1)
        assert len(rows_old) == 1
        assert rows_old[0]["action_type"] == "old_act"

    def test_limit_respected(self, audit_store):
        for i in range(20):
            audit_store.record_admin_action("flood")
        rows = audit_store.query_admin_actions(limit=5)
        assert len(rows) <= 5

    def test_outcome_filter(self, audit_store):
        audit_store.record_admin_action("ok_act", outcome="success")
        audit_store.record_admin_action("fail_act", outcome="failure")
        rows = audit_store.query_admin_actions(outcome="failure")
        assert len(rows) == 1
        assert rows[0]["action_type"] == "fail_act"


class TestAuditStoreToolExecutions:
    def test_record_and_query_tool_execution(self, audit_store):
        exec_id = audit_store.record_tool_execution(
            "read_file", success=True, pack="fs_tools",
            risk_level="read_only", duration_ms=17,
        )
        assert exec_id

        rows = audit_store.query_tool_executions(tool_name="read_file")
        assert len(rows) == 1
        assert rows[0]["success"] == 1
        assert rows[0]["duration_ms"] == 17

    def test_success_filter(self, audit_store):
        audit_store.record_tool_execution("ok_tool", success=True)
        audit_store.record_tool_execution("fail_tool", success=False, error="boom")
        ok_rows = audit_store.query_tool_executions(success_only=True)
        assert all(r["success"] == 1 for r in ok_rows)
        fail_rows = audit_store.query_tool_executions(success_only=False)
        assert all(r["success"] == 0 for r in fail_rows)

    def test_pack_filter(self, audit_store):
        audit_store.record_tool_execution("t1", success=True, pack="pack_a")
        audit_store.record_tool_execution("t2", success=True, pack="pack_b")
        rows = audit_store.query_tool_executions(pack="pack_a")
        assert len(rows) == 1

    def test_error_stored(self, audit_store):
        audit_store.record_tool_execution("failing", success=False, error="timeout")
        rows = audit_store.query_tool_executions(tool_name="failing")
        assert rows[0]["error"] == "timeout"


class TestAuditStoreSummary:
    def test_summary_counts(self, audit_store):
        audit_store.record_admin_action("act")
        audit_store.record_tool_execution("t", success=True)
        audit_store.record_tool_execution("t", success=False, error="e")

        s = audit_store.summary()
        assert s["admin_actions_total"] == 1
        assert s["tool_executions_total"] == 2
        assert s["tool_executions_ok"] == 1
        assert s["tool_executions_failed"] == 1

    def test_summary_empty(self, audit_store):
        s = audit_store.summary()
        assert s["admin_actions_total"] == 0
        assert s["tool_executions_total"] == 0
