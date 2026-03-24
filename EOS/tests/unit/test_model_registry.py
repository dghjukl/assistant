from __future__ import annotations

from pathlib import Path

from runtime.model_registry import collect_model_issues, migrate_models_config, validate_role_model_config


def _base_cfg() -> dict:
    return {
        "deployment_mode": "standard",
        "servers": {
            "primary": {"model_path": "models/primary/"},
            "vision": {"model_path": "models/vision/", "mmproj_path": "models/vision/"},
            "tool": {"model_path": "models/tools/"},
            "thinking": {"model_path": "models/thinking/"},
            "creativity": {"model_path": "models/creativity/"},
        },
    }


def test_migration_normalizes_legacy_tool_directory():
    cfg = migrate_models_config(_base_cfg())
    assert cfg["models"]["tools"]["local_path"] == "models/tools/"
    assert cfg["servers"]["tool"]["model_path"] == "models/tools/"


def test_builtin_primary_choice_a_sets_stable_path():
    cfg = _base_cfg()
    cfg["models"] = {
        "primary": {"source_type": "builtin", "builtin_id": "qwen3_14b_q5_k_m", "local_path": None}
    }
    migrated = migrate_models_config(cfg)
    primary = migrated["models"]["primary"]
    assert primary["local_path"] == "models/primary/Qwen3-14B-Q5_K_M.gguf"
    assert primary["url"].endswith("Qwen3-14B-Q5_K_M.gguf")


def test_builtin_primary_choice_b_sets_stable_path():
    cfg = _base_cfg()
    cfg["models"] = {
        "primary": {"source_type": "builtin", "builtin_id": "qwen3_8b_q5_k_m", "local_path": None}
    }
    migrated = migrate_models_config(cfg)
    primary = migrated["models"]["primary"]
    assert primary["local_path"] == "models/primary/Qwen3-8B-Q5_K_M.gguf"


def test_user_provided_primary_keeps_user_source_type():
    cfg = _base_cfg()
    cfg["models"] = {"primary": {"source_type": "user", "builtin_id": None, "local_path": "D:/models/p.gguf"}}
    migrated = migrate_models_config(cfg)
    assert migrated["models"]["primary"]["source_type"] == "user"
    assert migrated["models"]["primary"]["local_path"] == "D:/models/p.gguf"


def test_builtin_vision_assigns_model_and_mmproj_paths():
    cfg = _base_cfg()
    cfg["models"] = {"vision": {"source_type": "builtin", "builtin_id": "qwen25_vl_3b_f16"}}
    migrated = migrate_models_config(cfg)
    vision = migrated["models"]["vision"]
    assert vision["model_path"].endswith("Qwen2.5-VL-3B-Instruct-f16.gguf")
    assert vision["mmproj_path"].endswith("mmproj-Qwen2.5-VL-3B-Instruct-f16.gguf")


def test_user_vision_requires_both_paths(tmp_path: Path):
    cfg = _base_cfg()
    cfg["servers"]["vision"]["mmproj_path"] = None
    cfg["models"] = {"vision": {"source_type": "user", "builtin_id": None, "model_path": str(tmp_path / "vision.gguf"), "mmproj_path": None}}
    migrated = migrate_models_config(cfg)
    issues = validate_role_model_config(migrated, tmp_path, "vision")
    assert any("mmproj_path is required" in issue for issue in issues)


def test_tools_role_supports_builtin_or_user_only():
    cfg = _base_cfg()
    cfg["models"] = {"tools": {"source_type": "builtin", "builtin_id": "lfm2_1p2b_tool_q5_k_m"}}
    migrated = migrate_models_config(cfg)
    assert migrated["models"]["tools"]["builtin_id"] == "lfm2_1p2b_tool_q5_k_m"


def test_thinking_and_creativity_support_both_builtin_ids():
    cfg = _base_cfg()
    cfg["models"] = {
        "thinking": {"source_type": "builtin", "builtin_id": "qwen3_4b_q5_k_m"},
        "creativity": {"source_type": "builtin", "builtin_id": "lfm25_1p2b_thinking_q5_k_m"},
    }
    migrated = migrate_models_config(cfg)
    assert migrated["models"]["thinking"]["local_path"].endswith("Qwen3-4B-Q5_K_M.gguf")
    assert migrated["models"]["creativity"]["local_path"].endswith("LFM2.5-1.2B-Thinking-Q5_K_M.gguf")


def test_missing_user_path_error_is_role_specific(tmp_path: Path):
    cfg = _base_cfg()
    cfg["models"] = {
        "primary": {"source_type": "user", "builtin_id": None, "local_path": "models/primary/missing.gguf"},
        "thinking": {"source_type": "user", "builtin_id": None, "local_path": "models/thinking/existing.gguf"},
    }
    (tmp_path / "models" / "thinking").mkdir(parents=True)
    (tmp_path / "models" / "thinking" / "existing.gguf").write_text("x")
    migrated = migrate_models_config(cfg)
    primary_issues = validate_role_model_config(migrated, tmp_path, "primary")
    thinking_issues = validate_role_model_config(migrated, tmp_path, "thinking")
    assert any(issue.startswith("primary:") for issue in primary_issues)
    assert thinking_issues == []


def test_collect_model_issues_only_reports_enabled_roles(tmp_path: Path):
    cfg = _base_cfg()
    cfg["servers"]["primary"]["enabled"] = True
    cfg["servers"]["thinking"]["enabled"] = False
    cfg["models"] = {
        "primary": {"source_type": "user", "builtin_id": None, "local_path": "models/primary/missing.gguf"},
        "thinking": {"source_type": "user", "builtin_id": None, "local_path": "models/thinking/missing.gguf"},
    }
    migrated = migrate_models_config(cfg)
    issues = collect_model_issues(migrated, tmp_path, enabled_only=True)
    assert "primary" in issues
    assert "thinking" not in issues
