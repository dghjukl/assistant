from __future__ import annotations

import asyncio
import json
import sys
import types

from core.worldview import WorldviewService


class _FakeTopology:
    def primary_endpoint(self) -> str:
        return "http://example.test"


def _make_cfg(tmp_path):
    return {
        "project_root": str(tmp_path),
        "worldview": {
            "enabled": True,
            "worldview_path": "data/worldview",
            "max_profile_lines_in_prompt": 8,
        },
    }


def _write_source(service: WorldviewService, name: str, content: str) -> None:
    source_path = service._root / "sources" / name
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(content, encoding="utf-8")


def _write_source_bytes(service: WorldviewService, name: str, content: bytes) -> None:
    source_path = service._root / "sources" / name
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(content)


def test_worldview_extraction_lifecycle_initial_run(tmp_path):
    service = WorldviewService(_make_cfg(tmp_path))
    _write_source(service, "values.md", "I value truth, patience, and careful reasoning.")

    captured = {}

    async def _extractor(payload):
        captured["payload"] = payload
        return "# Partner Worldview Profile\n\n## Core Values\n- Tends to prize truth and patience.\n"

    result = asyncio.run(service.refresh_profile_from_sources(_extractor, trigger={"source": "test"}))

    assert result["status"] == "updated"
    assert result["profile_updated"] is True
    assert len(result["changed_files"]) == 1
    assert captured["payload"]["documents_to_process"][0]["relative_path"] == "values.md"
    assert "careful reasoning" in captured["payload"]["documents_to_process"][0]["content"]

    profile_path = service._root / "profile.md"
    profile_text = profile_path.read_text(encoding="utf-8")
    assert "Tends to prize truth and patience" in profile_text

    log = json.loads((service._root / "extraction_log.json").read_text(encoding="utf-8"))
    assert log["last_run"]["changed_file_count"] == 1
    assert log["last_run"]["trigger"]["source"] == "test"
    assert log["processed_files"][0]["relative_path"] == "values.md"
    assert log["processed_files"][0]["sha256"]


def test_worldview_extraction_detects_changed_files_and_noops_when_clean(tmp_path):
    service = WorldviewService(_make_cfg(tmp_path))
    _write_source(service, "values.md", "I care about rigor and honesty.")

    initial_profiles = []

    async def _first_extractor(payload):
        initial_profiles.append(payload["existing_profile"])
        return "# Partner Worldview Profile\n\n## Core Values\n- Often emphasizes rigor.\n"

    asyncio.run(service.refresh_profile_from_sources(_first_extractor, trigger={"source": "first"}))
    assert service.enumerate_changed_sources() == []

    noop = asyncio.run(service.refresh_profile_from_sources(_first_extractor, trigger={"source": "noop"}))
    assert noop["status"] == "noop"
    assert noop["reason"] == "no_changes"

    _write_source(service, "values.md", "I care about rigor, honesty, and compassionate action.")
    changed = service.enumerate_changed_sources()
    assert len(changed) == 1
    assert changed[0]["status"] == "changed"

    seen_existing = []

    async def _second_extractor(payload):
        seen_existing.append(payload["existing_profile"])
        return "# Partner Worldview Profile\n\n## Core Values\n- Often emphasizes rigor and compassion.\n"

    updated = asyncio.run(service.refresh_profile_from_sources(_second_extractor, trigger={"source": "second"}))
    assert updated["status"] == "updated"
    assert "Often emphasizes rigor" in seen_existing[0]

    log = json.loads((service._root / "extraction_log.json").read_text(encoding="utf-8"))
    assert log["last_run"]["changed_file_count"] == 1
    assert log["last_run"]["changed_files"][0]["status"] == "changed"
    assert log["processed_files"][0]["sha256"] == changed[0]["sha256"]


def test_worldview_block_pending_sources_is_internal_only(tmp_path):
    service = WorldviewService(_make_cfg(tmp_path))
    _write_source(service, "values.md", "I value truth, patience, and careful reasoning.")

    block = service.worldview_block().lower()

    assert "pending extraction" in block
    assert "should not trigger unprompted acknowledgment" in block
    assert "only mention pending material if explicitly asked" in block
    assert "worldview_read" in block
    assert "ask your partner" not in block
    assert "would like you to run extraction" not in block


def test_worldview_block_without_sources_preserves_human_triggered_extraction(tmp_path):
    service = WorldviewService(_make_cfg(tmp_path))

    block = service.worldview_block().lower()

    assert "ask for extraction" in block
    assert "ask your partner" not in block


def test_worldview_extraction_rejects_non_utf8_source_documents(tmp_path):
    service = WorldviewService(_make_cfg(tmp_path))
    _write_source_bytes(service, "latin1.txt", b"caf\xe9")

    async def _extractor(payload):
        raise AssertionError("extractor should not be invoked for unsupported source encodings")

    try:
        asyncio.run(service.refresh_profile_from_sources(_extractor, trigger={"source": "test"}))
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected refresh_profile_from_sources to reject non-UTF-8 sources")

    assert "UTF-8 plain text or Markdown" in message
    assert "latin1.txt" in message


def test_execute_worldview_extraction_uses_runtime_workflow(monkeypatch):
    import runtime.orchestrator as orch

    prompts = []
    fake_server = types.ModuleType("webui.server")

    class _FakeWorldviewService:
        async def refresh_profile_from_sources(self, extractor, *, trigger=None):
            payload = {
                "existing_profile": None,
                "documents_to_process": [{
                    "filename": "values.md",
                    "relative_path": "values.md",
                    "size_bytes": 10,
                    "sha256": "abc123",
                    "content": "I value truth.",
                }],
                "changed_files": [{
                    "filename": "values.md",
                    "relative_path": "values.md",
                    "size_bytes": 10,
                    "sha256": "abc123",
                    "content": "I value truth.",
                }],
                "all_sources": [{
                    "filename": "values.md",
                    "relative_path": "values.md",
                    "size_bytes": 10,
                    "mtime": "2026-03-23T00:00:00Z",
                    "mtime_ns": 1,
                    "sha256": "abc123",
                }],
                "profile_template": "# Partner Worldview Profile",
                "worldview_root": "/tmp/worldview",
            }
            generated = await extractor(payload)
            return {
                "status": "updated",
                "changed_files": payload["documents_to_process"],
                "processed_files": payload["all_sources"],
                "profile_updated": True,
                "profile_path": "/tmp/worldview/profile.md",
                "generated_profile": generated,
                "trigger": trigger,
            }

    fake_server._worldview_service = _FakeWorldviewService()
    fake_package = types.ModuleType("webui")
    fake_package.server = fake_server
    monkeypatch.setitem(sys.modules, "webui", fake_package)
    monkeypatch.setitem(sys.modules, "webui.server", fake_server)

    async def _fake_call_qwen3(topology, user_message, cfg, **kwargs):
        prompts.append(user_message)
        assert kwargs["use_think"] is True
        return "# Partner Worldview Profile\n\n## Core Values\n- Tends to value truth.\n"

    monkeypatch.setattr(orch, "call_qwen3", _fake_call_qwen3)

    result = asyncio.run(orch.execute_worldview_extraction(
        _FakeTopology(),
        "Update the worldview profile from the latest worldview sources.",
        {},
        entity_snapshot=None,
        trigger_source="chat",
    ))

    assert result["status"] == "updated"
    assert "refreshed the worldview profile" in result["message"].lower()
    assert prompts
    assert "I value truth." in prompts[0]
    assert orch._is_worldview_processing_request("Please update the worldview profile from worldview sources.")
