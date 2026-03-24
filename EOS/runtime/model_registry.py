from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

MODEL_ROLES: tuple[str, ...] = ("primary", "vision", "tools", "thinking", "creativity")
ROLE_TO_SERVER = {
    "primary": "primary",
    "vision": "vision",
    "tools": "tool",
    "thinking": "thinking",
    "creativity": "creativity",
}
SERVER_TO_ROLE = {v: k for k, v in ROLE_TO_SERVER.items()}

BUILTIN_MODELS: dict[str, list[dict[str, Any]]] = {
    "primary": [
        {
            "id": "qwen3_14b_q5_k_m",
            "filename": "Qwen3-14B-Q5_K_M.gguf",
            "url": "https://huggingface.co/bartowski/Qwen_Qwen3-14B-GGUF/resolve/main/Qwen3-14B-Q5_K_M.gguf",
        },
        {
            "id": "qwen3_8b_q5_k_m",
            "filename": "Qwen3-8B-Q5_K_M.gguf",
            "url": "https://huggingface.co/bartowski/Qwen_Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q5_K_M.gguf",
        },
    ],
    "vision": [
        {
            "id": "qwen25_vl_3b_f16",
            "model_filename": "Qwen2.5-VL-3B-Instruct-f16.gguf",
            "model_url": "https://huggingface.co/ggml-org/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/Qwen2.5-VL-3B-Instruct-f16.gguf",
            "mmproj_filename": "mmproj-Qwen2.5-VL-3B-Instruct-f16.gguf",
            "mmproj_url": "https://huggingface.co/ggml-org/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/mmproj-Qwen2.5-VL-3B-Instruct-f16.gguf",
        }
    ],
    "tools": [
        {
            "id": "lfm2_1p2b_tool_q5_k_m",
            "filename": "LFM2-1.2B-Tool-Q5_K_M.gguf",
            "url": "https://huggingface.co/bartowski/LiquidAI_LFM2-1.2B-Tool-GGUF/resolve/main/LFM2-1.2B-Tool-Q5_K_M.gguf",
        }
    ],
    "thinking": [
        {
            "id": "lfm25_1p2b_thinking_q5_k_m",
            "filename": "LFM2.5-1.2B-Thinking-Q5_K_M.gguf",
            "url": "https://huggingface.co/NexaAI/LFM2.5-1.2B-thinking-GGUF/resolve/main/LFM2.5-1.2B-Thinking-Q5_K_M.gguf",
        },
        {
            "id": "qwen3_4b_q5_k_m",
            "filename": "Qwen3-4B-Q5_K_M.gguf",
            "url": "https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q5_K_M.gguf",
        },
    ],
    "creativity": [
        {
            "id": "lfm25_1p2b_thinking_q5_k_m",
            "filename": "LFM2.5-1.2B-Thinking-Q5_K_M.gguf",
            "url": "https://huggingface.co/NexaAI/LFM2.5-1.2B-thinking-GGUF/resolve/main/LFM2.5-1.2B-Thinking-Q5_K_M.gguf",
        },
        {
            "id": "qwen3_4b_q5_k_m",
            "filename": "Qwen3-4B-Q5_K_M.gguf",
            "url": "https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q5_K_M.gguf",
        },
    ],
}


def _default_role_entry(role: str) -> dict[str, Any]:
    if role == "vision":
        return {
            "source_type": "user",
            "builtin_id": None,
            "model_url": None,
            "mmproj_url": None,
            "model_path": None,
            "mmproj_path": None,
        }
    return {
        "source_type": "user",
        "builtin_id": None,
        "url": None,
        "local_path": None,
    }


def _find_builtin(role: str, builtin_id: str | None) -> dict[str, Any] | None:
    if not builtin_id:
        return None
    for item in BUILTIN_MODELS.get(role, []):
        if item["id"] == builtin_id:
            return item
    return None


def _normalize_role_paths(path: str | None, role: str) -> str | None:
    if not path:
        return path
    normalized = path.replace("\\", "/")
    if role == "tools":
        if "models/tools" not in normalized:
            normalized = normalized.replace("models/tool", "models/tools")
    return normalized


def migrate_models_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Normalize config into the role-based models schema while preserving legacy fields."""
    out = deepcopy(cfg)
    out.setdefault("models", {})
    servers = out.setdefault("servers", {})

    for role in MODEL_ROLES:
        role_cfg = out["models"].get(role) or _default_role_entry(role)
        srv_role = ROLE_TO_SERVER[role]
        srv_cfg = dict(servers.get(srv_role) or {})

        # Legacy migration from servers.* paths.
        if role == "vision":
            if role_cfg.get("model_path") is None:
                role_cfg["model_path"] = _normalize_role_paths(srv_cfg.get("model_path"), role)
            if role_cfg.get("mmproj_path") is None:
                role_cfg["mmproj_path"] = _normalize_role_paths(srv_cfg.get("mmproj_path"), role)
        else:
            if role_cfg.get("local_path") is None:
                role_cfg["local_path"] = _normalize_role_paths(srv_cfg.get("model_path"), role)

        role_cfg["source_type"] = "builtin" if role_cfg.get("source_type") == "builtin" else "user"

        builtin = _find_builtin(role, role_cfg.get("builtin_id"))
        if role_cfg["source_type"] == "builtin" and builtin:
            if role == "vision":
                role_cfg["model_url"] = builtin["model_url"]
                role_cfg["mmproj_url"] = builtin["mmproj_url"]
                role_cfg["model_path"] = f"models/vision/{builtin['model_filename']}"
                role_cfg["mmproj_path"] = f"models/vision/{builtin['mmproj_filename']}"
            else:
                role_cfg["url"] = builtin["url"]
                role_cfg["local_path"] = f"models/{role}/{builtin['filename']}"
        else:
            # Keep explicit fields for user supplied paths.
            if role == "vision":
                role_cfg.setdefault("model_url", None)
                role_cfg.setdefault("mmproj_url", None)
            else:
                role_cfg.setdefault("url", None)

        out["models"][role] = role_cfg

        # Keep servers in sync for runtime components that still read these fields.
        if srv_cfg:
            if role == "vision":
                srv_cfg["model_path"] = role_cfg.get("model_path")
                srv_cfg["mmproj_path"] = role_cfg.get("mmproj_path")
            else:
                srv_cfg["model_path"] = role_cfg.get("local_path")
            servers[srv_role] = srv_cfg

    return out


def validate_role_model_config(cfg: dict[str, Any], root: Path, role: str) -> list[str]:
    issues: list[str] = []
    models = cfg.get("models", {})
    role_cfg = models.get(role, {})
    source_type = role_cfg.get("source_type")

    if source_type not in {"builtin", "user"}:
        issues.append(f"{role}: invalid source_type {source_type!r}")
        return issues

    if role == "vision":
        model_path = role_cfg.get("model_path")
        mmproj_path = role_cfg.get("mmproj_path")
        if not model_path:
            issues.append("vision: model_path is required")
        if not mmproj_path:
            issues.append("vision: mmproj_path is required")
        for label, path in (("model_path", model_path), ("mmproj_path", mmproj_path)):
            if path and not ((root / path).is_file() or Path(path).is_absolute() and Path(path).is_file()):
                issues.append(f"vision: {label} does not exist: {path}")
    else:
        local_path = role_cfg.get("local_path")
        if not local_path:
            issues.append(f"{role}: local_path is required")
        elif not ((root / local_path).is_file() or Path(local_path).is_absolute() and Path(local_path).is_file()):
            issues.append(f"{role}: local_path does not exist: {local_path}")

    return issues



def collect_model_issues(cfg: dict[str, Any], root: Path, *, enabled_only: bool = True) -> dict[str, list[str]]:
    """Return role->issues for role-based model assignments."""
    issues: dict[str, list[str]] = {}
    servers = cfg.get("servers", {})
    for role in MODEL_ROLES:
        srv_key = ROLE_TO_SERVER[role]
        srv_cfg = servers.get(srv_key, {})
        if enabled_only and not bool(srv_cfg.get("enabled", False)):
            continue
        role_issues = validate_role_model_config(cfg, root, role)
        if role_issues:
            issues[role] = role_issues
    return issues
