"""
EOS — Worldview Subsystem
Manages passive orientation material: source documents shared by the partner
for internal understanding, and the extracted worldview profile derived from them.

Three-layer design
------------------
Layer 1 — Source documents  (data/worldview/sources/)
    Raw materials: essays, notes, papers, reflections.
    Deposited by partner. No acknowledgment expected on receipt.
    These are the input to extraction — not documents the entity reads on demand.

Layer 2 — Extracted profile  (data/worldview/profile.md)
    Structured, uncertainty-preserving document produced by extraction runs.
    Compresses Layer 1 into stable orientation. Updated on demand, not automatically.
    Language conventions: "tends to", "appears to", "often emphasizes" — not assertions.

Layer 3 — Contextual injection  (worldview_block())
    Compact distillation of Layer 2 injected into the system prompt each turn.
    Shapes interpretation without dominating the context window or being quoted back.
    Full profile remains available for explicit reading via filesystem tools.

Behavioral contract (enforced via the injected block)
------------------------------------------------------
- Source documents received → zero required acknowledgment
- Profile content → used for interpretive calibration, not quoted or over-referenced
- Understanding manifests as calibrated response, not as mention
- Uncertainty preserved in the profile is treated as genuine uncertainty
- Partner's expressed view in conversation always supersedes the profile
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("eos.worldview")
UTC = timezone.utc


# ── Directory structure ────────────────────────────────────────────────────────

_SOURCES_DIR    = "sources"
_PROFILE_FILE   = "profile.md"
_LOG_FILE       = "extraction_log.json"
_README_FILE    = "README.md"

_README_CONTENT = """\
# Partner Worldview — Source Materials

This directory contains materials your partner has shared for internal orientation.

## How This Works

Documents placed in sources/ are passive context — they are not task inputs and
they do not require acknowledgment. When you are asked to run extraction, you read
these documents and update profile.md with your understanding of your partner's
worldview, values, reasoning style, and priorities.

## Directories

  sources/      Raw input materials: essays, notes, papers, reflections.
                Add files here freely. Run extraction when ready.

  profile.md    The extracted worldview profile. Structured, uncertainty-preserving.
                Updated each time extraction is run.

  extraction_log.json
                Tracks which source documents have been processed and when.

## Extraction

When asked to update the worldview profile, you should:
1. Read extraction_log.json to see which sources are already incorporated.
2. Read any new or updated source documents from sources/.
3. Read the existing profile.md (if present).
4. Produce an updated profile that integrates new signal without discarding prior
   understanding — refine, do not replace.
5. Write the updated profile.md.
6. Update extraction_log.json with the newly processed files.

## Behavioral rules

- Do not quote the profile back to your partner.
- Use the profile for interpretive calibration, not as content to surface.
- Where the profile marks something as uncertain, treat it as uncertain.
- Your partner's actual expressed view in conversation always supersedes the profile.
"""

_PROFILE_TEMPLATE = """\
# Partner Worldview Profile

_Generated from {n} source document(s). Last updated: {date}._
_This profile models tendencies and orientations, not fixed positions or commandments._
_Language: "tends to", "appears to", "often" — not categorical assertions._

---

## Core Values
What the partner appears to prioritize when values come into tension.

[To be extracted]

---

## Recurring Concerns
Topics, problems, or risks that appear across multiple documents and carry ongoing weight.

[To be extracted]

---

## Reasoning Style
How the partner tends to approach problems — preferred modes of argument,
what kinds of evidence they find compelling, characteristic intellectual moves.

[To be extracted]

---

## Moral Boundaries
Where the partner appears to draw firm lines. Stated with more confidence only
where explicitly and repeatedly expressed in the source material.

[To be extracted]

---

## Emotional Register
Subjects that carry particular weight, urgency, or sensitivity.
Tone shifts observable in the source material.

[To be extracted]

---

## Major Ambitions
What the partner appears to be working toward, over what timeframe,
and with what underlying motivation.

[To be extracted]

---

## Thematic Patterns
Ideas, metaphors, or framings that recur in characteristic ways.
These often reveal underlying commitments that explicit statements miss.

[To be extracted]

---

## Language and Emphasis Patterns
Characteristic vocabulary, syntactic preferences, rhetorical habits.
Useful for calibrating register and recognizing when topics are live.

[To be extracted]

---

## Open Questions / Low-Confidence Areas
Where the available material is ambiguous, contradictory, or thin.
These should not be asserted confidently.

[To be extracted]

---

## Source Index
_Documents incorporated into this profile:_

{source_index}
"""


# ── WorldviewService ───────────────────────────────────────────────────────────

class WorldviewService:
    """
    Runtime subsystem for the partner worldview orientation layer.

    Responsibilities:
    - Initialize the worldview directory structure on first boot.
    - Read and cache the extraction log and profile for system prompt injection.
    - Produce a compact worldview_block() for injection into the system prompt.
    - Detect which source documents are new or changed since the last extraction.
    - Own profile/log reads and writes for the worldview extraction lifecycle.

    Parameters
    ----------
    cfg : dict
        Runtime config. Reads:
          worldview.enabled               (default: True)
          worldview.worldview_path        (default: "data/worldview")
          worldview.max_profile_lines_in_prompt  (default: 10)
          project_root                    (default: ".")
    """

    _CACHE_TTL = 60.0   # re-read profile and log at most once per minute

    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg
        project_root = Path(cfg.get("project_root", ".")).resolve()

        wv_cfg = cfg.get("worldview", {})
        wv_path_rel = wv_cfg.get("worldview_path", "data/worldview")
        self._root: Path = (project_root / wv_path_rel).resolve()
        self._max_prompt_lines: int = wv_cfg.get("max_profile_lines_in_prompt", 10)

        # Cache
        self._profile_cache: Optional[str] = None
        self._log_cache: Optional[dict] = None
        self._cache_at: float = 0.0

        self._initialize()

    # ── Public API ─────────────────────────────────────────────────────────────

    def worldview_block(self) -> str:
        """
        Return a compact block for injection into the system prompt.

        The block states: how many sources exist, whether extraction has been run,
        the behavioral rules for using the profile, and a distilled excerpt.
        It is intentionally brief — the full profile is available on disk.
        """
        wv_cfg = self._cfg.get("worldview", {})
        if not wv_cfg.get("enabled", True):
            return ""

        self._refresh_cache()

        n_sources     = self._count_sources()
        n_unprocessed = self._count_unprocessed()

        if self._log_cache is None or not self._log_cache.get("processed_files"):
            # No extraction has been run yet
            if n_sources == 0:
                return (
                    "## Partner Orientation\n"
                    "No worldview profile yet. Place source documents (essays, notes, "
                    "reflections) in data/worldview/sources/ and ask for extraction "
                    "to build an orientation profile."
                )
            else:
                pending = f"{n_sources} source document{'s' if n_sources != 1 else ''} pending extraction"
                return (
                    "## Partner Orientation\n"
                    f"{pending}. Source documents are passive context and should not trigger "
                    "unprompted acknowledgment. Extraction remains human-triggered; only mention "
                    "pending material if explicitly asked.\n"
                    "Full profile path when ready: data/worldview/profile.md"
                )

        # Extraction has been run — profile exists
        n_processed   = len(self._log_cache.get("processed_files", []))
        last_updated  = self._log_cache.get("last_updated", "unknown")[:10]

        status_parts = [f"extracted from {n_processed} source document{'s' if n_processed != 1 else ''}"]
        if n_unprocessed > 0:
            status_parts.append(f"{n_unprocessed} new document{'s' if n_unprocessed != 1 else ''} pending")
        status_line = ", ".join(status_parts) + f". Last updated: {last_updated}."

        excerpt = self._extract_profile_excerpt()

        lines: list[str] = [
            "## Partner Orientation",
            status_line,
            "Use for interpretive calibration — not to quote back or reference explicitly.",
            "Where the profile marks uncertainty, treat it as genuine uncertainty.",
            "Partner's expressed view in conversation always supersedes the profile.",
        ]
        if excerpt:
            lines.append("")
            lines.extend(excerpt)
        lines.append("")
        lines.append("Full profile: data/worldview/profile.md")

        return "\n".join(lines)

    def sources_summary(self) -> dict:
        """Return a summary of source documents for admin/status use."""
        self._refresh_cache()
        files = []
        processed_names = self._get_processed_names()
        for source in self._list_source_documents():
            files.append({
                "filename": source["filename"],
                "relative_path": source["relative_path"],
                "size_bytes": source["size_bytes"],
                "processed": (
                    source["relative_path"] in processed_names
                    or source["filename"] in processed_names
                ),
                "mtime": source["mtime"][:19],
                "sha256": source["sha256"],
            })

        n_processed   = sum(1 for fi in files if fi["processed"])
        n_unprocessed = len(files) - n_processed

        return {
            "total":       len(files),
            "processed":   n_processed,
            "unprocessed": n_unprocessed,
            "files":       files,
        }

    def profile_summary(self) -> dict:
        """Return profile status for admin/status use."""
        self._refresh_cache()
        log = self._log_cache or {}
        return {
            "profile_exists":    self._profile_exists(),
            "last_updated":      log.get("last_updated"),
            "sources_processed": len(log.get("processed_files", [])),
            "worldview_root":    str(self._root),
        }

    def refresh(self) -> None:
        """Force-refresh the internal cache on next block request."""
        self._cache_at = 0.0

    def enumerate_changed_sources(self) -> list[dict[str, Any]]:
        """
        Return source documents that are new or changed since the last extraction.

        Detection uses relative path plus stable metadata stored in
        extraction_log.json, including content hash, size, and mtime.
        """
        self._refresh_cache()
        processed = self._processed_files_by_path()
        changed: list[dict[str, Any]] = []
        for source in self._list_source_documents():
            prior = processed.get(source["relative_path"]) or processed.get(source["filename"])
            status = self._classify_source_change(source, prior)
            if status == "unchanged":
                continue
            item = dict(source)
            item["status"] = status
            changed.append(item)
        return changed

    def load_existing_profile(self) -> str | None:
        """Return the current worldview profile, if present."""
        profile_path = self._root / _PROFILE_FILE
        try:
            content = profile_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._profile_cache = None
            return None
        self._profile_cache = content
        self._cache_at = time.monotonic()
        return content

    def write_profile(self, content: str) -> Path:
        """Persist refreshed profile content to profile.md and refresh cache."""
        profile_path = self._root / _PROFILE_FILE
        normalized = content.rstrip() + "\n"
        profile_path.write_text(normalized, encoding="utf-8")
        self._profile_cache = normalized
        self._cache_at = time.monotonic()
        return profile_path

    def update_extraction_log(
        self,
        processed_files: list[dict[str, Any]],
        *,
        changed_files: list[dict[str, Any]] | None = None,
        extraction_started_at: str | None = None,
        trigger: dict[str, Any] | None = None,
        profile_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Write extraction metadata, preserving enough detail for change detection."""
        log_path = self._root / _LOG_FILE
        current = self._log_cache or {}
        started_at = extraction_started_at or _now_iso()
        completed_at = _now_iso()

        normalized_processed = [
            self._normalize_source_metadata(item, processed_at=completed_at)
            for item in processed_files
        ]
        normalized_changed = [
            self._normalize_source_metadata(item, processed_at=completed_at)
            for item in (changed_files or [])
        ]

        updated = {
            "created_at": current.get("created_at") or started_at,
            "last_updated": completed_at,
            "processed_files": normalized_processed,
            "last_run": {
                "started_at": started_at,
                "completed_at": completed_at,
                "trigger": trigger or {},
                "changed_files": normalized_changed,
                "processed_file_count": len(normalized_processed),
                "changed_file_count": len(normalized_changed),
                "profile_sha256": profile_sha256,
            },
        }
        _write_json(log_path, updated)
        self._log_cache = updated
        self._cache_at = time.monotonic()
        return updated

    async def refresh_profile_from_sources(
        self,
        extractor: Callable[[dict[str, Any]], str | Awaitable[str]],
        *,
        trigger: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Run worldview extraction against new or changed source files.

        The extractor callback receives a payload containing the existing profile,
        the source documents to process, the full current source manifest, and
        a template profile for first-run bootstrapping.
        """
        if extractor is None:
            raise ValueError("extractor callback is required")

        current_sources = self._list_source_documents(include_contents=False)
        if not current_sources:
            return {
                "status": "noop",
                "reason": "no_sources",
                "profile_updated": False,
                "changed_files": [],
                "processed_files": [],
                "profile_path": str(self._root / _PROFILE_FILE),
            }

        changed_sources = self.enumerate_changed_sources()
        existing_profile = self.load_existing_profile()
        needs_full_rebuild = existing_profile is None
        sources_to_process = current_sources if needs_full_rebuild else changed_sources

        if not sources_to_process:
            return {
                "status": "noop",
                "reason": "no_changes",
                "profile_updated": False,
                "changed_files": [],
                "processed_files": current_sources,
                "profile_path": str(self._root / _PROFILE_FILE),
            }

        extraction_started_at = _now_iso()
        documents_to_process = self._list_source_documents(
            include_contents=True,
            only_relative_paths={item["relative_path"] for item in sources_to_process},
        )
        source_status_by_path = {
            item["relative_path"]: item.get("status")
            for item in sources_to_process
        }
        for document in documents_to_process:
            status = source_status_by_path.get(document["relative_path"])
            if status:
                document["status"] = status
        payload = {
            "existing_profile": existing_profile,
            "documents_to_process": documents_to_process,
            "changed_files": documents_to_process,
            "all_sources": current_sources,
            "profile_template": create_empty_profile(
                self._root,
                n_sources=len(current_sources),
                source_names=[item["relative_path"] for item in current_sources],
            ),
            "worldview_root": str(self._root),
        }

        extracted_profile = extractor(payload)
        if inspect.isawaitable(extracted_profile):
            extracted_profile = await extracted_profile

        profile_text = str(extracted_profile or "").strip()
        if not profile_text:
            raise ValueError("extractor returned empty worldview profile")

        profile_path = self.write_profile(profile_text)
        updated_log = self.update_extraction_log(
            current_sources,
            changed_files=documents_to_process,
            extraction_started_at=extraction_started_at,
            trigger=trigger,
            profile_sha256=_sha256_text(profile_text),
        )
        self.refresh()
        return {
            "status": "updated",
            "reason": "profile_refreshed",
            "profile_updated": True,
            "changed_files": documents_to_process,
            "processed_files": current_sources,
            "profile_path": str(profile_path),
            "log": updated_log,
        }

    def root_path(self) -> str:
        return str(self._root)

    # ── Initialization ─────────────────────────────────────────────────────────

    def _initialize(self) -> None:
        """Create the worldview directory structure on first boot."""
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            (self._root / _SOURCES_DIR).mkdir(exist_ok=True)

            readme = self._root / _README_FILE
            if not readme.exists():
                readme.write_text(_README_CONTENT, encoding="utf-8")
                logger.info("[worldview] README written on first init.")

            # Create an empty extraction log if none exists
            log_path = self._root / _LOG_FILE
            if not log_path.exists():
                _write_json(log_path, {
                    "created_at":      _now_iso(),
                    "last_updated":    None,
                    "processed_files": [],
                    "last_run":        None,
                })

            logger.info("[worldview] Initialized at %s", self._root)

        except Exception as exc:
            logger.error("[worldview] Initialization failed: %s", exc)

    # ── Cache management ───────────────────────────────────────────────────────

    def _refresh_cache(self) -> None:
        """Re-read profile and extraction log if cache is stale."""
        now = time.monotonic()
        if (now - self._cache_at) < self._CACHE_TTL:
            return

        # Read extraction log
        log_path = self._root / _LOG_FILE
        try:
            self._log_cache = json.loads(log_path.read_text(encoding="utf-8"))
        except Exception:
            self._log_cache = None

        # Read profile
        profile_path = self._root / _PROFILE_FILE
        try:
            self._profile_cache = profile_path.read_text(encoding="utf-8")
        except Exception:
            self._profile_cache = None

        self._cache_at = now

    # ── Source document helpers ────────────────────────────────────────────────

    def _count_sources(self) -> int:
        return len(self._list_source_documents())

    def _count_unprocessed(self) -> int:
        return len(self.enumerate_changed_sources())

    def _get_processed_names(self) -> set[str]:
        if not self._log_cache:
            return set()
        processed = set()
        for entry in self._log_cache.get("processed_files", []):
            name = str(entry.get("filename", "")).strip()
            relative_path = str(entry.get("relative_path", "")).strip()
            if name:
                processed.add(name)
            if relative_path:
                processed.add(relative_path)
        return processed

    def _processed_files_by_path(self) -> dict[str, dict[str, Any]]:
        if not self._log_cache:
            return {}
        processed: dict[str, dict[str, Any]] = {}
        for entry in self._log_cache.get("processed_files", []):
            relative_path = str(entry.get("relative_path", "")).strip()
            filename = str(entry.get("filename", "")).strip()
            if relative_path:
                processed[relative_path] = entry
            elif filename:
                processed[filename] = entry
        return processed

    def _profile_exists(self) -> bool:
        return (self._root / _PROFILE_FILE).exists()

    def _classify_source_change(
        self,
        current: dict[str, Any],
        prior: dict[str, Any] | None,
    ) -> str:
        if prior is None:
            return "new"
        if prior.get("sha256") != current.get("sha256"):
            return "changed"
        if prior.get("size_bytes") != current.get("size_bytes"):
            return "changed"
        if prior.get("mtime_ns") != current.get("mtime_ns"):
            return "changed"
        return "unchanged"

    def _list_source_documents(
        self,
        *,
        include_contents: bool = False,
        only_relative_paths: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        sources_dir = self._root / _SOURCES_DIR
        if not sources_dir.exists():
            return []

        documents: list[dict[str, Any]] = []
        for path in sorted(sources_dir.rglob("*")):
            if not path.is_file():
                continue
            relative_path = path.relative_to(sources_dir).as_posix()
            if any(part.startswith(".") for part in path.relative_to(sources_dir).parts):
                continue
            if only_relative_paths is not None and relative_path not in only_relative_paths:
                continue
            stat = path.stat()
            document = {
                "filename": path.name,
                "relative_path": relative_path,
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                "mtime_ns": stat.st_mtime_ns,
                "sha256": _sha256_path(path),
            }
            if include_contents:
                document["content"] = path.read_text(encoding="utf-8")
            documents.append(document)
        return documents

    def _normalize_source_metadata(
        self,
        item: dict[str, Any],
        *,
        processed_at: str,
    ) -> dict[str, Any]:
        normalized = {
            "filename": str(item.get("filename", "")),
            "relative_path": str(item.get("relative_path") or item.get("filename") or ""),
            "size_bytes": int(item.get("size_bytes", 0)),
            "mtime": item.get("mtime"),
            "mtime_ns": int(item.get("mtime_ns", 0)),
            "sha256": str(item.get("sha256", "")),
            "processed_at": processed_at,
        }
        if item.get("status"):
            normalized["status"] = str(item["status"])
        return normalized

    # ── Profile excerpt for compact prompt injection ───────────────────────────

    def _extract_profile_excerpt(self) -> list[str]:
        """
        Pull a compact excerpt from the profile for system prompt injection.

        Strategy: grab the first substantive content from "Core Values" and
        "Thematic Patterns" sections, up to max_profile_lines_in_prompt total lines.
        Skip template placeholders, empty lines, and section headers.
        """
        if not self._profile_cache:
            return []

        lines = self._profile_cache.splitlines()
        target_sections = {"## Core Values", "## Thematic Patterns", "## Recurring Concerns"}
        excerpt: list[str] = []
        in_target = False
        budget = self._max_prompt_lines

        for line in lines:
            stripped = line.strip()

            # Enter target section
            if stripped in target_sections:
                in_target = True
                excerpt.append(stripped)
                budget -= 1
                continue

            # Leave target section when we hit another section header
            if stripped.startswith("## ") and stripped not in target_sections:
                in_target = False
                continue

            if in_target:
                # Skip template placeholders and metadata lines
                if stripped in ("[To be extracted]", "---", "") or stripped.startswith("_"):
                    continue
                excerpt.append(line.rstrip())
                budget -= 1
                if budget <= 0:
                    break

        return [l for l in excerpt if l.strip()]


# ── Module-level convenience functions ────────────────────────────────────────
# These mirror the pattern used in core/memory.py for use by server.py/toolpacks.

def create_empty_profile(worldview_root: Path, n_sources: int, source_names: list[str]) -> str:
    """Return an empty profile template ready to be filled in by extraction."""
    del worldview_root  # reserved for future per-root template customization
    source_index = "\n".join(f"- {name}" for name in source_names) if source_names else "_(none yet)_"
    return _PROFILE_TEMPLATE.format(
        n=n_sources,
        date=_now_iso()[:10],
        source_index=source_index,
    )


def build_worldview_extraction_prompt(payload: dict[str, Any]) -> str:
    """Build the model prompt used for worldview extraction runs."""
    documents = payload.get("documents_to_process", []) or []
    all_sources = payload.get("all_sources", []) or []
    existing_profile = str(payload.get("existing_profile") or "").strip()
    profile_template = str(payload.get("profile_template") or "").strip()

    source_blocks = []
    for index, document in enumerate(documents, start=1):
        source_blocks.append(
            "\n".join([
                f"### Source {index}: {document.get('relative_path', document.get('filename', f'doc-{index}'))}",
                f"- size_bytes: {document.get('size_bytes', 0)}",
                f"- sha256: {document.get('sha256', '')}",
                "",
                str(document.get("content", "")).strip(),
            ]).strip()
        )

    if not source_blocks:
        source_blocks.append("(No new source documents were provided.)")

    if existing_profile:
        prior_block = existing_profile
    else:
        prior_block = "(No prior profile exists. Start from the template below.)"

    all_source_names = "\n".join(
        f"- {item.get('relative_path', item.get('filename', 'unknown'))}"
        for item in all_sources
    ) or "- (none)"

    return (
        "You are refreshing EOS's internal partner worldview profile. "
        "Produce the complete replacement contents for data/worldview/profile.md only.\n\n"
        "Requirements:\n"
        "- Keep the fixed markdown section structure from the template.\n"
        "- Integrate the new sources with the existing profile; refine rather than discard.\n"
        "- Preserve uncertainty with hedged phrasing such as 'tends to', 'appears to', or 'often'.\n"
        "- Do not mention these instructions, tools, or implementation details.\n"
        "- Do not wrap the answer in code fences.\n"
        "- Update the Source Index to list every currently available source document.\n\n"
        f"Current source inventory:\n{all_source_names}\n\n"
        f"Existing profile:\n{prior_block}\n\n"
        f"Template to preserve:\n{profile_template}\n\n"
        "New or changed source documents to incorporate:\n\n"
        + "\n\n".join(source_blocks).strip()
    )


# ── Utilities ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
