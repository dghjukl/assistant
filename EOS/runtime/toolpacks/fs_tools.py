"""File System Tools — Read, write, and manage files within allowed roots

Provides tools for:
- File existence and stat info
- Directory creation and listing
- Reading (head, tail, grep)
- Copying and moving (with safeguards)
- Deleting (destructive, gated)
- Zip operations

Configuration:
  fs_tools:
    enabled: true
    allow_destructive: false        # Set true to enable delete/move operations
    allowed_roots:
      - data                        # Paths are relative to project root unless absolute
      - logs
      - config
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _jdump(x: Any) -> str:
    """JSON dump with fallback to str()."""
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def _safe_int(x: Any, default: int) -> int:
    """Safe integer conversion."""
    try:
        return int(x)
    except Exception:
        return default


def _now_iso() -> str:
    """Current time in ISO format."""
    return datetime.now().isoformat(timespec="seconds")


def register(registry: Any, config: Dict[str, Any]) -> None:
    """Register file system tools into the registry."""
    from runtime.tool_registry import (
        ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy
    )

    fs_cfg = config.get("fs", {}) if isinstance(config, dict) else {}
    enabled = bool(fs_cfg.get("enabled", True))
    allow_destructive = bool(fs_cfg.get("allow_destructive", False))

    # Allowed roots are relative to project root unless absolute
    allowed_roots = fs_cfg.get("allowed_roots", ["data", "logs", "config"])
    if isinstance(allowed_roots, str):
        allowed_roots = [r.strip() for r in allowed_roots.split(",") if r.strip()]
    if not isinstance(allowed_roots, list) or not allowed_roots:
        allowed_roots = ["data", "logs", "config"]

    project_root = Path(config.get("project_root", ".")).resolve()

    allowed_abs: List[Path] = []
    for r in allowed_roots:
        try:
            rp = Path(str(r))
            if rp.is_absolute():
                allowed_abs.append(rp.resolve())
            else:
                allowed_abs.append((project_root / rp).resolve())
        except Exception:
            continue

    def _is_allowed(p: Path) -> bool:
        try:
            p = p.resolve()
        except Exception:
            return False
        for ar in allowed_abs:
            try:
                if hasattr(p, "is_relative_to") and p.is_relative_to(ar):
                    return True
                if str(p).lower().startswith(str(ar).lower().rstrip("\\/") + os.sep.lower()):
                    return True
                if str(p).lower() == str(ar).lower():
                    return True
            except Exception:
                continue
        return False

    def _resolve_path(path_str: str) -> Tuple[Optional[Path], Optional[str]]:
        if not path_str:
            return None, "Missing path."
        p = Path(path_str)
        if not p.is_absolute():
            p = project_root / p
        try:
            p = p.resolve()
        except Exception as e:
            return None, f"Could not resolve path: {e}"
        if not _is_allowed(p):
            return None, f"Path not allowed: {p}"
        return p, None

    def _iter_files(root: Path, depth: int) -> Iterable[Path]:
        root = root.resolve()
        if depth < 0:
            return []
        base_parts = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            dp = Path(dirpath)
            d = len(dp.parts) - base_parts
            if d > depth:
                dirnames[:] = []
                continue
            for fn in filenames:
                yield dp / fn

    # ── Read-only tools ─────────────────────────────────────────────────────

    def file_exists_handler(params: Dict[str, Any]) -> str:
        p, err = _resolve_path(str(params.get("path") or "").strip())
        if err:
            return _jdump({"error": err})
        return _jdump({"path": str(p), "exists": p.exists()})

    registry.register(ToolSpec(
        name="file_exists",
        description="Check whether a path exists.",
        pack="fs_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=file_exists_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def stat_path_handler(params: Dict[str, Any]) -> str:
        p, err = _resolve_path(str(params.get("path") or "").strip())
        if err:
            return _jdump({"error": err})
        if not p.exists():
            return _jdump({"path": str(p), "exists": False})
        st = p.stat()
        return _jdump({
            "path": str(p),
            "exists": True,
            "is_dir": p.is_dir(),
            "size_bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })

    registry.register(ToolSpec(
        name="stat_path",
        description="Return stat info (size, mtime, is_dir).",
        pack="fs_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=stat_path_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def glob_search_handler(params: Dict[str, Any]) -> str:
        root, err = _resolve_path(str(params.get("root") or "").strip())
        if err:
            return _jdump({"error": err})
        pattern = str(params.get("pattern") or "*").strip() or "*"
        depth = _safe_int(params.get("depth"), 4)
        depth = max(0, min(depth, 24))
        max_results = _safe_int(params.get("max_results"), 200)
        max_results = max(1, min(max_results, 2000))

        hits: List[str] = []
        for fp in _iter_files(root, depth):
            rel = str(fp.relative_to(root))
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(fp.name, pattern):
                hits.append(str(fp))
                if len(hits) >= max_results:
                    break
        return _jdump({
            "root": str(root),
            "pattern": pattern,
            "depth": depth,
            "count": len(hits),
            "paths": hits,
        })

    registry.register(ToolSpec(
        name="glob_search",
        description="Search files using glob patterns (depth-limited).",
        pack="fs_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string"},
                "pattern": {"type": "string"},
                "depth": {"type": "integer"},
                "max_results": {"type": "integer"},
            },
            "required": ["root"],
        },
        handler=glob_search_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def head_file_handler(params: Dict[str, Any]) -> str:
        p, err = _resolve_path(str(params.get("path") or "").strip())
        if err:
            return _jdump({"error": err})
        lines = _safe_int(params.get("lines"), 50)
        lines = max(1, min(lines, 500))
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                out = []
                for _ in range(lines):
                    line = f.readline()
                    if not line:
                        break
                    out.append(line.rstrip("\n"))
            return _jdump({"path": str(p), "lines": out})
        except Exception as e:
            return _jdump({"error": f"head_file failed: {e}"})

    registry.register(ToolSpec(
        name="head_file",
        description="Read first N lines from a text file.",
        pack="fs_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "lines": {"type": "integer"},
            },
            "required": ["path"],
        },
        handler=head_file_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def tail_file_handler(params: Dict[str, Any]) -> str:
        p, err = _resolve_path(str(params.get("path") or "").strip())
        if err:
            return _jdump({"error": err})
        lines = _safe_int(params.get("lines"), 50)
        lines = max(1, min(lines, 500))
        try:
            from collections import deque
            with p.open("r", encoding="utf-8", errors="replace") as f:
                buf = deque(f, maxlen=lines)
            out = [x.rstrip("\n") for x in buf]
            return _jdump({"path": str(p), "lines": out})
        except Exception as e:
            return _jdump({"error": f"tail_file failed: {e}"})

    registry.register(ToolSpec(
        name="tail_file",
        description="Read last N lines from a text file.",
        pack="fs_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "lines": {"type": "integer"},
            },
            "required": ["path"],
        },
        handler=tail_file_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def grep_file_handler(params: Dict[str, Any]) -> str:
        p, err = _resolve_path(str(params.get("path") or "").strip())
        if err:
            return _jdump({"error": err})
        patt = str(params.get("pattern") or "").strip()
        if not patt:
            return _jdump({"error": "Missing pattern."})
        is_regex = bool(params.get("regex", False))
        max_matches = _safe_int(params.get("max_matches"), 200)
        max_matches = max(1, min(max_matches, 5000))

        rx = None
        if is_regex:
            try:
                rx = re.compile(patt)
            except Exception as e:
                return _jdump({"error": f"Invalid regex: {e}"})

        matches: List[Dict[str, Any]] = []
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, start=1):
                    s = line.rstrip("\n")
                    ok = (rx.search(s) is not None) if rx else (patt in s)
                    if ok:
                        matches.append({"line": i, "text": s[:500]})
                        if len(matches) >= max_matches:
                            break
            return _jdump({
                "path": str(p),
                "pattern": patt,
                "regex": is_regex,
                "count": len(matches),
                "matches": matches,
            })
        except Exception as e:
            return _jdump({"error": f"grep_file failed: {e}"})

    registry.register(ToolSpec(
        name="grep_file",
        description="Search a file for a substring or regex.",
        pack="fs_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "pattern": {"type": "string"},
                "regex": {"type": "boolean"},
                "max_matches": {"type": "integer"},
            },
            "required": ["path", "pattern"],
        },
        handler=grep_file_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    # ── Write tools (reversible) ────────────────────────────────────────────

    def make_dir_handler(params: Dict[str, Any]) -> str:
        p, err = _resolve_path(str(params.get("path") or "").strip())
        if err:
            return _jdump({"error": err})
        parents = bool(params.get("parents", True))
        exist_ok = bool(params.get("exist_ok", True))
        try:
            p.mkdir(parents=parents, exist_ok=exist_ok)
            return _jdump({"path": str(p), "created": True})
        except Exception as e:
            return _jdump({"error": str(e)})

    registry.register(ToolSpec(
        name="make_dir",
        description="Create a directory.",
        pack="fs_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "parents": {"type": "boolean"},
                "exist_ok": {"type": "boolean"},
            },
            "required": ["path"],
        },
        handler=make_dir_handler,
        risk_level=ToolRiskLevel.REVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.SOFT_CONFIRM,
        enabled=enabled,
    ))

    def copy_path_handler(params: Dict[str, Any]) -> str:
        src, err = _resolve_path(str(params.get("src") or "").strip())
        if err:
            return _jdump({"error": err})
        dst, err = _resolve_path(str(params.get("dst") or "").strip())
        if err:
            return _jdump({"error": err})
        overwrite = bool(params.get("overwrite", False))

        if dst.exists() and not overwrite:
            return _jdump({"error": f"Destination exists (set overwrite=true): {dst}"})
        if dst.exists() and dst.is_dir() and overwrite and not allow_destructive:
            return _jdump({"error": "Destructive ops disabled (fs.allow_destructive)"})

        try:
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            return _jdump({"src": str(src), "dst": str(dst), "copied": True})
        except Exception as e:
            return _jdump({"error": str(e)})

    registry.register(ToolSpec(
        name="copy_path",
        description="Copy file/dir (non-destructive unless overwrite).",
        pack="fs_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {
                "src": {"type": "string"},
                "dst": {"type": "string"},
                "overwrite": {"type": "boolean"},
            },
            "required": ["src", "dst"],
        },
        handler=copy_path_handler,
        risk_level=ToolRiskLevel.REVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.SOFT_CONFIRM,
        enabled=enabled,
    ))

    # ── Destructive tools (gated) ───────────────────────────────────────────

    def move_path_handler(params: Dict[str, Any]) -> str:
        if not allow_destructive:
            return _jdump({"error": "Destructive ops disabled (fs.allow_destructive)"})
        src, err = _resolve_path(str(params.get("src") or "").strip())
        if err:
            return _jdump({"error": err})
        dst, err = _resolve_path(str(params.get("dst") or "").strip())
        if err:
            return _jdump({"error": err})
        overwrite = bool(params.get("overwrite", False))

        if dst.exists() and not overwrite:
            return _jdump({"error": f"Destination exists (set overwrite=true): {dst}"})

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and overwrite:
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            shutil.move(str(src), str(dst))
            return _jdump({"src": str(src), "dst": str(dst), "moved": True})
        except Exception as e:
            return _jdump({"error": str(e)})

    registry.register(ToolSpec(
        name="move_path",
        description="Move file/dir (destructive).",
        pack="fs_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {
                "src": {"type": "string"},
                "dst": {"type": "string"},
                "overwrite": {"type": "boolean"},
            },
            "required": ["src", "dst"],
        },
        handler=move_path_handler,
        risk_level=ToolRiskLevel.IRREVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.HARD_CONFIRM,
        enabled=enabled and allow_destructive,
    ))

    def delete_file_handler(params: Dict[str, Any]) -> str:
        if not allow_destructive:
            return _jdump({"error": "Destructive ops disabled (fs.allow_destructive)"})
        p, err = _resolve_path(str(params.get("path") or "").strip())
        if err:
            return _jdump({"error": err})
        if not p.exists():
            return _jdump({"error": f"Not found: {p}"})
        if p.is_dir():
            return _jdump({"error": f"Path is a directory (use delete_dir): {p}"})

        try:
            p.unlink()
            return _jdump({"path": str(p), "deleted": True})
        except Exception as e:
            return _jdump({"error": str(e)})

    registry.register(ToolSpec(
        name="delete_file",
        description="Delete a file (destructive).",
        pack="fs_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=delete_file_handler,
        risk_level=ToolRiskLevel.IRREVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.HARD_CONFIRM,
        enabled=enabled and allow_destructive,
    ))

    def delete_dir_handler(params: Dict[str, Any]) -> str:
        if not allow_destructive:
            return _jdump({"error": "Destructive ops disabled (fs.allow_destructive)"})
        p, err = _resolve_path(str(params.get("path") or "").strip())
        if err:
            return _jdump({"error": err})
        recursive = bool(params.get("recursive", False))
        if not p.exists():
            return _jdump({"error": f"Not found: {p}"})
        if not p.is_dir():
            return _jdump({"error": f"Path is not a directory: {p}"})

        try:
            if recursive:
                shutil.rmtree(p)
            else:
                p.rmdir()
            return _jdump({"path": str(p), "deleted": True})
        except Exception as e:
            return _jdump({"error": str(e)})

    registry.register(ToolSpec(
        name="delete_dir",
        description="Delete a directory (destructive).",
        pack="fs_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "recursive": {"type": "boolean"},
            },
            "required": ["path"],
        },
        handler=delete_dir_handler,
        risk_level=ToolRiskLevel.IRREVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.HARD_CONFIRM,
        enabled=enabled and allow_destructive,
    ))

    # ── Archive tools ───────────────────────────────────────────────────────

    def zip_create_handler(params: Dict[str, Any]) -> str:
        root, err = _resolve_path(str(params.get("root") or "").strip())
        if err:
            return _jdump({"error": err})
        out_zip, err = _resolve_path(str(params.get("output_zip") or "").strip())
        if err:
            return _jdump({"error": err})

        include_globs = params.get("include_globs", ["**/*"])
        exclude_globs = params.get("exclude_globs", [])
        depth = _safe_int(params.get("depth"), 12)
        depth = max(0, min(depth, 24))

        if isinstance(include_globs, str):
            include_globs = [include_globs]
        if isinstance(exclude_globs, str):
            exclude_globs = [exclude_globs]

        def included(rel: str) -> bool:
            inc = any(fnmatch.fnmatch(rel, g) for g in include_globs) if include_globs else True
            exc = any(fnmatch.fnmatch(rel, g) for g in exclude_globs) if exclude_globs else False
            return inc and not exc

        files: List[Path] = []
        base_parts = len(root.parts)
        for fp in _iter_files(root, depth):
            rel = str(fp.relative_to(root)).replace("\\", "/")
            dp = len(fp.parent.resolve().parts) - base_parts
            if dp > depth:
                continue
            if included(rel):
                files.append(fp)

        try:
            out_zip.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
                for fp in files:
                    arc = str(fp.relative_to(root)).replace("\\", "/")
                    z.write(fp, arcname=arc)
            return _jdump({
                "root": str(root),
                "output_zip": str(out_zip),
                "files": len(files),
                "created_at": _now_iso(),
            })
        except Exception as e:
            return _jdump({"error": str(e)})

    registry.register(ToolSpec(
        name="zip_create",
        description="Create a zip from files (glob include/exclude).",
        pack="fs_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {
                "root": {"type": "string"},
                "output_zip": {"type": "string"},
                "include_globs": {"type": ["array", "string"]},
                "exclude_globs": {"type": ["array", "string"]},
                "depth": {"type": "integer"},
            },
            "required": ["root", "output_zip"],
        },
        handler=zip_create_handler,
        risk_level=ToolRiskLevel.REVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.SOFT_CONFIRM,
        enabled=enabled,
    ))

    def zip_extract_handler(params: Dict[str, Any]) -> str:
        zpath, err = _resolve_path(str(params.get("zip_path") or "").strip())
        if err:
            return _jdump({"error": err})
        dest, err = _resolve_path(str(params.get("dest_root") or "").strip())
        if err:
            return _jdump({"error": err})

        try:
            dest.mkdir(parents=True, exist_ok=True)
            extracted = 0
            with zipfile.ZipFile(zpath, "r") as z:
                for member in z.infolist():
                    target = (dest / member.filename).resolve()
                    if not _is_allowed(target) or not str(target).startswith(str(dest.resolve())):
                        continue
                    z.extract(member, path=dest)
                    extracted += 1
            return _jdump({
                "zip_path": str(zpath),
                "dest_root": str(dest),
                "extracted": extracted,
                "extracted_at": _now_iso(),
            })
        except Exception as e:
            return _jdump({"error": str(e)})

    registry.register(ToolSpec(
        name="zip_extract",
        description="Extract a zip (zip-slip protected).",
        pack="fs_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {
                "zip_path": {"type": "string"},
                "dest_root": {"type": "string"},
            },
            "required": ["zip_path", "dest_root"],
        },
        handler=zip_extract_handler,
        risk_level=ToolRiskLevel.REVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.SOFT_CONFIRM,
        enabled=enabled,
    ))
