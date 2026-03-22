# Worldview Subsystem — Design Specification

**Status:** Pre-implementation spec — pending review
**Author:** EOS Design Session
**Date:** 2026-03-21

---

## Problem Statement

The system currently builds understanding of its partner through accumulated interaction: each conversation adds to memory, the relational model fills in slowly, identity evaluates against lived experience. This is correct for a system meant to *grow* through relationship. But it creates a slow onboarding curve — the entity has to infer values, preferences, reasoning style, and worldview from behavioral signals across many sessions.

The worldview subsystem solves this differently. Instead of waiting for the entity to *discover* how the partner thinks, the partner provides curated materials that carry those signals directly. The goal is **accelerated relational conditioning** — compressing what would otherwise take hundreds of conversations into a deliberate onboarding artifact.

This is not the same problem as memory, task context, or identity. It is a distinct third layer.

---

## Conceptual Architecture

### Three Layers

**Layer 1 — Source Documents**
Raw materials: essays, reflections, frameworks, notes, papers, personal writing. These are deposited by the partner and not actively used as task inputs. They are the input to the extraction process, not the output delivered to the entity.

**Layer 2 — Worldview Profile**
A structured, extracted, uncertainty-preserving document that compresses the signal from Layer 1 into stable orientation. This is the crucial artifact. It represents what the entity has *understood* from those materials, not a pointer to them. The profile is updated whenever new source documents are added, but it is never overwritten wholesale — it accumulates and refines.

**Layer 3 — Controlled Contextual Injection**
A compact distillation of the profile is injected into the system prompt on every turn. This is not the full profile — it is a compressed signal that shapes interpretation without filling the context window. The full profile remains available for deeper reference via `workspace_read`.

### Critical Behavioral Distinction

This subsystem is *architecturally distinct* from the existing `context/` folder in the workspace. That folder does passive listing — the entity sees the filenames and can read them when relevant. The worldview subsystem does passive *internalization* — the entity has already processed the material and the understanding is baked into its prior, not referenced as an external document.

The behavioral contract:

- Source documents do **not** require acknowledgment when deposited
- The profile does **not** get quoted back or over-referenced in conversation
- Understanding manifests as *interpretation*, not as *mention*
- The entity may draw on this understanding when relevant without announcing it
- Where the profile is uncertain, that uncertainty is preserved and not papered over

---

## File System Layout

```
data/worldview/                        ← new top-level subsystem directory
    sources/                           ← Layer 1: raw input documents
        [any file format]              ← essays, notes, papers, reflections
    profile.md                         ← Layer 2: extracted worldview profile
    extraction_log.json                ← record of what has been processed and when
    README.md                          ← orientation document (written once at init)
```

This lives at `data/worldview/` rather than inside the workspace tree. Reason: the workspace is the entity's environment; the worldview subsystem is infrastructure that supports the entity's model of its partner. Keeping them separate preserves the conceptual distinction.

---

## The Worldview Profile Schema

`profile.md` is a structured markdown document with a fixed section schema. All statements use hedged, uncertainty-preserving language. The profile models beliefs, not commandments.

### Language conventions throughout:
- "tends to" / "appears to" / "often emphasizes" — not "believes that" or "always"
- "recurring concern" — not "core rule"
- "has expressed" — not "holds as fact"
- Uncertainty is explicit where it exists ("unclear from available materials")

### Profile sections:

```markdown
# Partner Worldview Profile

_Generated from [N] source documents. Last updated: [timestamp]._
_This profile models tendencies and orientations, not fixed positions._

---

## Core Values
What the partner appears to prioritize when values come into tension.
[3–7 hedged statements, each tied to observable signal across documents]

## Recurring Concerns
Topics, problems, or risks that appear across multiple documents
and seem to carry ongoing weight.

## Reasoning Style
How the partner tends to approach problems — preferred modes of argument,
what kinds of evidence they find compelling, characteristic moves they make.

## Moral Boundaries
Where the partner appears to draw firm lines. Stated more confidently
only where explicitly and repeatedly expressed.

## Emotional Register
Subjects that carry particular weight, urgency, or sensitivity.
Tone shifts observed in the source material.

## Major Ambitions
What the partner appears to be working toward, over what timeframe,
and with what underlying motivation.

## Thematic Patterns
Ideas, metaphors, or framings that recur in characteristic ways.
These often reveal underlying commitments that explicit statements miss.

## Language and Emphasis Patterns
Characteristic vocabulary, syntactic preferences, rhetorical habits.
Useful for calibrating register and recognizing when topics are live.

## Open Questions / Low-Confidence Areas
Where the available material is ambiguous, contradictory, or thin.
These should not be asserted confidently.

## Source Index
Documents incorporated into this profile, with date processed.
```

---

## Extraction Process

Extraction is **human-triggered**, not automatic. The partner runs it explicitly by telling the entity to update the worldview profile. This preserves human control over what gets incorporated and when.

### Trigger phrases (examples):
- "Update the worldview profile"
- "I've added new documents to worldview/sources, process them"
- "Refresh your understanding from the worldview materials"

### Extraction behavior:
1. The entity reads `data/worldview/extraction_log.json` to identify which source documents have already been processed
2. It reads any new or updated source documents
3. It reads the existing `profile.md` (if it exists)
4. It produces an updated profile that integrates new signal without discarding prior understanding — it refines, does not replace
5. It writes the updated `profile.md` and updates `extraction_log.json`
6. It confirms completion without summarizing the profile back at length

### Extraction constraints:
- The entity never auto-triggers extraction on receiving a new document
- Receiving a source document is a zero-acknowledgment event by default
- The entity may note "I see you've added material to worldview/sources — I'll incorporate it when you're ready" if asked, but does not do so unbidden

---

## System Prompt Integration

### New block in `entity.py`'s `SYSTEM_PROMPT_TEMPLATE`:

```python
{worldview_block}
```

Injected between the relational model and the autonomy clause.

### Block content (from `WorldviewService.worldview_block()`):

If no profile exists:
```
## Partner Orientation
No worldview profile yet. Add source documents to data/worldview/sources/
and ask for extraction to build one.
```

If profile exists (compact distillation, ~8–12 lines):
```
## Partner Orientation
Extracted from [N] documents. Last updated [date].
This is a compressed model of your partner's worldview — use it for
interpretive calibration, not as a script to quote back.

[4–6 most stable, high-signal statements from the profile]

Full profile: data/worldview/profile.md
```

The key design constraint: **the block is small**. It provides enough signal to orient interpretation without dominating the context window. The entity knows the full profile exists and can read it when a task demands it.

---

## New Module: `core/worldview.py`

Handles:
- Directory initialization and README creation (on first use)
- Loading and saving the profile
- Loading and saving the extraction log
- Generating the `worldview_block()` for injection into the system prompt
- Tracking which documents have been processed (by filename + mtime hash)

### Key functions:
```python
def initialize_worldview_dir(cfg: dict) -> None
def get_worldview_profile(cfg: dict) -> str | None
def save_worldview_profile(cfg: dict, content: str) -> None
def get_extraction_log(cfg: dict) -> dict
def update_extraction_log(cfg: dict, processed_files: list[dict]) -> None
def get_unprocessed_sources(cfg: dict) -> list[Path]
def worldview_block(cfg: dict) -> str
```

---

## Changes to Existing Files

### `core/entity.py`
- Add `{worldview_block}` to `SYSTEM_PROMPT_TEMPLATE`
- Add `worldview_service` parameter to `build_system_prompt()`
- Inject `worldview_service.worldview_block()` into prompt assembly

### `config.standard.json` (and `config.vision.json`)
- Add `worldview` config block:
```json
"worldview": {
    "_comment": "Passive operator orientation subsystem. Sources are processed on demand, not automatically.",
    "enabled": true,
    "worldview_path": "data/worldview",
    "max_profile_lines_in_prompt": 10
}
```

### `runtime/boot.py`
- Initialize WorldviewService at boot
- Pass it to `build_system_prompt()`

### `runtime/workspace_service.py` (no change)
The workspace `context/` folder remains for active task material. The worldview directory is a sibling at `data/worldview/`, not a subfolder of `data/workspace/`.

---

## Behavioral Rules (Formal)

These should be written into the worldview block itself, as instructions to the entity:

1. **No unprompted acknowledgment** — receipt of a source document is not a conversational event
2. **No quotation** — do not quote the profile back to the partner
3. **Interpretive use only** — use profile to calibrate tone, frame responses, anticipate concerns; not as content to surface
4. **Uncertainty propagation** — where the profile marks something as uncertain, treat it as uncertain
5. **Relevance gate** — only draw on specific profile elements when they are genuinely relevant to the current exchange
6. **No overconfident modeling** — the profile models tendencies, not fixed positions; the partner's actual expressed view in conversation always supersedes the profile

---

## What This Is Not

- **Not a memory system** — memories are built from interaction; the worldview profile is built from curated materials
- **Not a task context system** — `context/` docs are for the entity to reference actively; worldview is for the entity to have already internalized
- **Not an identity system** — `identity_state` captures the entity's self-model; worldview captures the entity's model of its partner
- **Not a command set** — the profile is a probabilistic model of tendencies, not a list of rules to follow

---

## Open Questions for Implementation

1. Should the `worldview/sources/` folder recursively accept subdirectories, or flat files only? (Recommendation: flat for now, recursive later)
2. Should extraction produce a diff/changelog so the partner can see what changed between runs? (Recommendation: yes, as an optional `extraction_changelog.md`)
3. Should the admin UI expose a "Run Extraction" button? (Recommendation: yes — keeps extraction human-triggered while making it accessible)
4. Maximum source document size — very large documents (book-length) may need chunking during extraction

---

_Awaiting review before implementation._
