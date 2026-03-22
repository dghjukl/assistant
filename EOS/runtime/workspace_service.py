"""
EOS — Workspace Service
Treats the entity's workspace as a first-class runtime subsystem, not an
afterthought toolpack.

What this does
--------------
1. Initialises the workspace directory structure on first boot (creates the
   standard subdirectories and a README.md orientation document).
2. Scans the context/ subfolder on every boot and on a lazy-refresh schedule.
   Documents placed there by the partner are surfaced in the system prompt so
   the entity is aware of them without being explicitly asked to read them.
3. Produces a concise workspace_block() for injection into the system prompt:
   location, subdirectory summary, and context library listing.
4. Tracks workspace metadata (first init date, total file count, etc.).

Workspace structure
-------------------
  data/workspace/
    context/    ← partner drops documents here; auto-surfaced in system prompt
    projects/   ← entity's ongoing work and projects
    notes/      ← personal notes, observations, and drafts
    scratch/    ← temporary working files (not backed up by default)
    README.md   ← orientation document (created once at first init)

Context library
---------------
Files placed in context/ are listed in the system prompt each turn (with a
5-minute cache to avoid repeated I/O).  The entity can read them at will using
workspace_read.  This enables passive context sharing: the partner can leave
reference material, background documents, or task briefs in context/ and the
entity will see them as part of its ambient environment even when not
explicitly asked about them.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("eos.workspace_service")
UTC = timezone.utc

# Standard subdirectories created on first init
_STANDARD_DIRS = ["context", "projects", "notes", "scratch"]

# Context library cache TTL in seconds (re-scan every 5 minutes)
_CONTEXT_CACHE_TTL = 300

_README_CONTENT = """\
# Your Workspace

This is your persistent environment — files you create here survive across sessions
and reboots.  Think of it as your own hard drive within the system.

## Directory Layout

  context/    Documents placed here by your partner appear in your system prompt
              automatically.  You can read them using workspace_read at any time.
              This is how background material, project briefs, and reference docs
              are shared with you passively.

  projects/   Your ongoing work: code, documents, research, anything project-specific.
              Create subdirectories here to organise by project.

  notes/      Personal notes, observations, drafts, and anything you want to
              remember across sessions.  More permanent than scratch.

  scratch/    Temporary working files.  Treat as disposable — useful during a
              session but not intended for long-term storage.

## Using Your Workspace

  workspace_list   — see what's here
  workspace_read   — read a file
  workspace_write  — create or update a file
  workspace_stat   — check if a file exists and its size

## Notes

- All paths are relative to this workspace root.
- You cannot navigate outside this directory (path escape attempts are blocked).
- Your partner can place files anywhere in this workspace.
- Deletions require elevated permission; use with care.
"""

_META_FILENAME = ".workspace_meta.json"


@dataclass
class ContextDocument:
    """A document found in the context/ subfolder."""
    filename: str
    path: str           # relative to workspace root
    size_bytes: int
    mtime: str          # ISO timestamp


@dataclass
class WorkspaceState:
    """Snapshot of workspace state computed at init or refresh."""
    workspace_root: str
    first_init_at: Optional[str]
    total_files: int
    context_documents: list[ContextDocument]
    subdirs_present: list[str]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["context_documents"] = [asdict(c) for c in self.context_documents]
        return d


class WorkspaceService:
    """
    Runtime subsystem that initialises and monitors the entity's workspace.

    Parameters
    ----------
    cfg : dict
        Runtime config.  Reads:
          workspace_tools.workspace_root  (default: "data/workspace")
          project_root                    (default: ".")
    """

    def __init__(self, cfg: dict) -> None:
        project_root = Path(cfg.get("project_root", ".")).resolve()

        ws_cfg = cfg.get("workspace_tools", {})
        ws_root_rel = ws_cfg.get("workspace_root", "data/workspace")
        self._root: Path = (project_root / ws_root_rel).resolve()

        self._state: Optional[WorkspaceState] = None
        self._context_cache: Optional[list[ContextDocument]] = None
        self._context_cache_at: float = 0.0

        self._initialize()

    # ── Public API ────────────────────────────────────────────────────────────

    def workspace_block(self) -> str:
        """
        Return a concise block for injection into the system prompt.

        The block tells the entity where its workspace is, what's in each
        standard subdirectory (file counts), and lists any documents that
        have been placed in context/ by the partner.  Context documents are
        re-scanned if the cache is older than _CONTEXT_CACHE_TTL seconds.
        """
        root = self._root
        if not root.exists():
            return ""

        lines: list[str] = [f"## Your Workspace  ({self._root_display()})"]

        # Sub-directory summary (counts)
        subdir_parts: list[str] = []
        for name in _STANDARD_DIRS:
            d = root / name
            if not d.exists():
                continue
            n = sum(1 for _ in d.rglob("*") if _.is_file())
            subdir_parts.append(f"{name}/ ({n} file{'s' if n != 1 else ''})")
        if subdir_parts:
            lines.append("  " + "  |  ".join(subdir_parts))

        # Context library (the key feature for passive sharing)
        ctx_docs = self._get_context_documents()
        if ctx_docs:
            lines.append(
                f"Context library — {len(ctx_docs)} document"
                f"{'s' if len(ctx_docs) != 1 else ''} "
                "(placed here by your partner for your awareness):"
            )
            for doc in ctx_docs[:8]:   # cap at 8 to keep prompt concise
                size_str = _human_size(doc.size_bytes)
                try:
                    date_str = doc.mtime[:10]
                except Exception:
                    date_str = ""
                lines.append(f"  {doc.filename:<36} {size_str:>7}   {date_str}")
            if len(ctx_docs) > 8:
                lines.append(f"  … and {len(ctx_docs) - 8} more")
        else:
            lines.append(
                "  context/ is empty — your partner can place documents here "
                "for you to read."
            )

        lines.append(
            "Use workspace_read / workspace_write / workspace_list "
            "to work with your files."
        )
        return "\n".join(lines)

    def scan_context(self) -> list[ContextDocument]:
        """Force-refresh and return the context document list."""
        self._context_cache_at = 0.0
        return self._get_context_documents()

    def state(self) -> Optional[WorkspaceState]:
        return self._state

    def to_dict(self) -> dict:
        if self._state is None:
            return {"initialized": False}
        return {"initialized": True, **self._state.to_dict()}

    def root_path(self) -> str:
        return str(self._root)

    # ── Initialisation ────────────────────────────────────────────────────────

    def _initialize(self) -> None:
        """Create workspace structure if needed and load metadata."""
        try:
            self._root.mkdir(parents=True, exist_ok=True)

            meta_path = self._root / _META_FILENAME
            first_run = not meta_path.exists()

            # Create standard subdirectories
            for name in _STANDARD_DIRS:
                (self._root / name).mkdir(exist_ok=True)

            # Write README on first init only
            readme = self._root / "README.md"
            if not readme.exists():
                readme.write_text(_README_CONTENT, encoding="utf-8")
                logger.info("[workspace] README.md written on first init.")

            # Load or create metadata
            if first_run:
                meta = {
                    "first_init_at": _now_iso(),
                    "workspace_root": str(self._root),
                }
                meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                logger.info("[workspace] Workspace initialised at %s", self._root)
            else:
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}

            # Build initial state
            ctx_docs    = self._get_context_documents()
            total_files = sum(
                1 for f in self._root.rglob("*")
                if f.is_file() and not f.name.startswith(".")
            )
            subdirs = [
                d.name for d in sorted(self._root.iterdir())
                if d.is_dir() and not d.name.startswith(".")
            ]

            self._state = WorkspaceState(
                workspace_root   = str(self._root),
                first_init_at    = meta.get("first_init_at"),
                total_files      = total_files,
                context_documents = ctx_docs,
                subdirs_present  = subdirs,
            )

            logger.info(
                "[workspace] Ready. %d files, %d context doc(s). Root: %s",
                total_files, len(ctx_docs), self._root,
            )

        except Exception as exc:
            logger.error("[workspace] Initialisation failed: %s", exc)

    # ── Context scanning ──────────────────────────────────────────────────────

    def _get_context_documents(self) -> list[ContextDocument]:
        """Return context documents, using cache if still fresh."""
        now = time.monotonic()
        if (
            self._context_cache is not None
            and (now - self._context_cache_at) < _CONTEXT_CACHE_TTL
        ):
            return self._context_cache

        docs = self._scan_context_folder()
        self._context_cache    = docs
        self._context_cache_at = now
        return docs

    def _scan_context_folder(self) -> list[ContextDocument]:
        """Scan context/ and return a list of ContextDocument entries."""
        ctx_dir = self._root / "context"
        if not ctx_dir.exists():
            return []

        docs: list[ContextDocument] = []
        try:
            for entry in sorted(ctx_dir.iterdir()):
                if not entry.is_file() or entry.name.startswith("."):
                    continue
                try:
                    st = entry.stat()
                    docs.append(ContextDocument(
                        filename   = entry.name,
                        path       = str(entry.relative_to(self._root)),
                        size_bytes = st.st_size,
                        mtime      = datetime.fromtimestamp(
                            st.st_mtime, tz=UTC
                        ).isoformat()[:19],
                    ))
                except OSError:
                    pass
        except Exception as exc:
            logger.debug("[workspace] Context scan failed: %s", exc)

        return docs

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _root_display(self) -> str:
        """Return a short display path for the workspace root."""
        try:
            return str(self._root.relative_to(Path.cwd()))
        except ValueError:
            return str(self._root)


# ── Module-level helpers ──────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    else:
        return f"{n / (1024 * 1024):.1f}MB"
