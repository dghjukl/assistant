# Partner Worldview — Source Materials

This directory contains materials your partner has shared for internal orientation.

## How This Works

Documents placed in `sources/` are **passive context** — they are not task inputs
and they do not require acknowledgment. When you are asked to run extraction, you
read these documents and update `profile.md` with your understanding of your
partner's worldview, values, reasoning style, and priorities.

## Directories

  `sources/`     Raw input materials: essays, notes, papers, reflections.
                 Add files here freely. Run extraction when ready.

  `profile.md`   The extracted worldview profile. Structured, uncertainty-preserving.
                 Updated each time extraction is run.

  `extraction_log.json`
                 Tracks which source documents have been processed and when.

## Extraction

When asked to update the worldview profile, you should:

1. Read `extraction_log.json` to see which sources are already incorporated.
2. Read any new or updated source documents from `sources/`.
3. Read the existing `profile.md` (if present).
4. Produce an updated profile that integrates new signal without discarding prior
   understanding — refine, do not replace.
5. Write the updated `profile.md`.
6. Update `extraction_log.json` with the newly processed files and current timestamp.

## Behavioral rules

- Do not quote the profile back to your partner.
- Use the profile for interpretive calibration, not as content to surface.
- Where the profile marks something as uncertain, treat it as uncertain.
- Your partner's actual expressed view in conversation always supersedes the profile.
- Receiving a source document is not a conversational event — no acknowledgment needed.
