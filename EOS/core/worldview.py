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

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
    - List unprocessed source documents so the entity knows what's pending.

    The service is read-only at runtime. The entity writes profile.md and
    updates extraction_log.json directly via filesystem tools during extraction.
    The service re-reads these files on each worldview_block() call (with caching).

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
                    f"{pending}. Ask your partner if they'd like you to run extraction "
                    "to build the worldview profile from these materials.\n"
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
        sources_dir = self._root / _SOURCES_DIR
        if not sources_dir.exists():
            return {"total": 0, "processed": 0, "unprocessed": 0, "files": []}

        self._refresh_cache()
        processed_names = self._get_processed_names()

        files = []
        for f in sorted(sources_dir.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            st = f.stat()
            files.append({
                "filename":   f.name,
                "size_bytes": st.st_size,
                "processed":  f.name in processed_names,
                "mtime":      datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat()[:19],
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
            "profile_exists":   self._profile_exists(),
            "last_updated":     log.get("last_updated"),
            "sources_processed": len(log.get("processed_files", [])),
            "worldview_root":   str(self._root),
        }

    def refresh(self) -> None:
        """Force-refresh the internal cache on next block request."""
        self._cache_at = 0.0

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
        sources_dir = self._root / _SOURCES_DIR
        if not sources_dir.exists():
            return 0
        return sum(
            1 for f in sources_dir.iterdir()
            if f.is_file() and not f.name.startswith(".")
        )

    def _count_unprocessed(self) -> int:
        sources_dir = self._root / _SOURCES_DIR
        if not sources_dir.exists():
            return 0
        processed = self._get_processed_names()
        return sum(
            1 for f in sources_dir.iterdir()
            if f.is_file() and not f.name.startswith(".") and f.name not in processed
        )

    def _get_processed_names(self) -> set[str]:
        if not self._log_cache:
            return set()
        return {
            entry.get("filename", "") for entry in self._log_cache.get("processed_files", [])
        }

    def _profile_exists(self) -> bool:
        return (self._root / _PROFILE_FILE).exists()

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
    source_index = "\n".join(f"- {name}" for name in source_names) if source_names else "_(none yet)_"
    return _PROFILE_TEMPLATE.format(
        n=n_sources,
        date=_now_iso()[:10],
        source_index=source_index,
    )


# ── Utilities ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
