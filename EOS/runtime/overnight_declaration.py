from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from dateutil import parser as dtparser


_DECLARATION_PATTERNS = [
    re.compile(r"\b(sign(?:ing)?\s+off|heading\s+off|going\s+offline|get(?:ting)?\s+off|log(?:ging)?\s+off|call(?:ing)?\s+it\s+a\s+night)\b", re.I),
    re.compile(r"\b(going\s+to\s+bed|bed\s+soon|get(?:ting)?\s+tired|turning\s+in|sleep(?:ing)?\s+in)\b", re.I),
    re.compile(r"\b(back\s+(?:around|on|by)|back\s+in\s+the\s+morning|back\s+tomorrow|until\s+\d|sleeping\s+in\s+until)\b", re.I),
]

_NEGATIVE_PATTERNS = [
    re.compile(r"\b(server|service|api|system|computer|network|app|website|bot)\s+(?:is|was|went)?\s*(?:offline|down|sleeping)\b", re.I),
    re.compile(r"\bbedtime\b.*\b(story|routine|song)\b", re.I),
]

_APPROX_WORDS = {"around", "about", "probably", "maybe", "roughly", "ish", "approximately", "let's say", "lets say"}
_RETURN_HINTS = ("back", "return", "tomorrow", "morning", "wake", "until", "sleeping in")
_AWAY_HINTS = ("off", "offline", "bed", "tired", "signing off", "heading off", "soon", "tonight")


@dataclass
class ExtractedOvernightDeclaration:
    is_declaration: bool
    away_start_time: str | None = None
    expected_return_time: str | None = None
    confidence: float = 0.0
    is_one_off: bool = True
    source_text: str = ""
    notes: dict[str, Any] | None = None
    acknowledgment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_declaration": self.is_declaration,
            "away_start_time": self.away_start_time,
            "expected_return_time": self.expected_return_time,
            "confidence": self.confidence,
            "is_one_off": self.is_one_off,
            "source_text": self.source_text,
            "notes": dict(self.notes or {}),
            "acknowledgment": self.acknowledgment,
        }


class OvernightDeclarationExtractor:
    """Deterministic-first conversational overnight declaration extractor."""

    def __init__(self, cfg: dict | None = None) -> None:
        self._cfg = cfg or {}
        ocfg = self._cfg.get("overnight_cycle", {})
        self._enabled = bool(ocfg.get("enabled", True))
        self._conversation_enabled = bool(ocfg.get("conversation_declare_enabled", True))
        self._soon_minutes = int(ocfg.get("default_soon_minutes", 30))
        self._morning_hour = float(ocfg.get("default_morning_return_hour", 8.0))
        self._fallback_enabled = bool(ocfg.get("llm_fallback_enabled", True))

    def extract(
        self,
        text: str,
        *,
        now: datetime | None = None,
        topology=None,
        cfg: dict | None = None,
    ) -> ExtractedOvernightDeclaration:
        now_dt = _ensure_utc(now or datetime.now(timezone.utc))
        source = str(text or "").strip()
        if not self._enabled or not self._conversation_enabled or not source:
            return ExtractedOvernightDeclaration(is_declaration=False, source_text=source)

        lowered = source.lower()
        if any(p.search(source) for p in _NEGATIVE_PATTERNS):
            return ExtractedOvernightDeclaration(is_declaration=False, source_text=source)

        declaration_hits = sum(1 for p in _DECLARATION_PATTERNS if p.search(source))
        if declaration_hits == 0:
            return ExtractedOvernightDeclaration(is_declaration=False, source_text=source)

        notes: dict[str, Any] = {
            "detector_hits": declaration_hits,
            "matched_patterns": [p.pattern for p in _DECLARATION_PATTERNS if p.search(source)],
        }

        away_start = self._extract_away_start(source, now_dt, notes)
        expected_return = self._extract_return_time(source, now_dt, away_start, notes)
        confidence = 0.46 + min(0.16 * declaration_hits, 0.24)

        if away_start is not None:
            confidence += 0.14
        if expected_return is not None:
            confidence += 0.18
        if any(word in lowered for word in _APPROX_WORDS):
            confidence -= 0.04
            notes["approximate_language"] = True

        if away_start is None and expected_return is not None and self._strong_sleep_context(lowered):
            away_start = now_dt + timedelta(minutes=self._soon_minutes)
            notes["away_start_inferred"] = "strong_sleep_context"
            confidence += 0.06
        elif away_start is None and expected_return is not None and any(anchor in lowered for anchor in ("heading off", "signing off", "getting off", "going offline")):
            away_start = now_dt + timedelta(minutes=self._soon_minutes)
            notes["away_start_inferred"] = "away_declaration_without_time"
            confidence += 0.04

        if away_start is not None and expected_return is None and self._strong_sleep_context(lowered):
            expected_return = self._default_morning_return(now_dt)
            notes["expected_return_inferred"] = "default_morning"
            confidence -= 0.04

        if away_start and expected_return and expected_return <= away_start:
            expected_return = self._repair_return_after_away(expected_return, away_start, lowered, notes)

        if (away_start is None or expected_return is None) and self._fallback_enabled:
            fallback = self._llm_fallback(source, now_dt, topology=topology, cfg=cfg or self._cfg)
            if fallback:
                notes["llm_fallback_used"] = True
                away_start = away_start or _parse_iso_datetime(fallback.get("away_start_time"))
                expected_return = expected_return or _parse_iso_datetime(fallback.get("expected_return_time"))
                confidence = max(confidence, float(fallback.get("confidence") or 0.0) * 0.75)
                notes["llm_fallback"] = fallback

        if away_start is None or expected_return is None:
            notes["missing_fields"] = {
                "away_start_time": away_start is None,
                "expected_return_time": expected_return is None,
            }
            if away_start is None and expected_return is None:
                return ExtractedOvernightDeclaration(
                    is_declaration=False,
                    source_text=source,
                    notes=notes,
                    confidence=min(confidence, 0.45),
                )

        away_iso = _iso(away_start) if away_start else None
        return_iso = _iso(expected_return) if expected_return else None
        confidence = max(0.0, min(confidence, 0.97))
        acknowledgment = self.build_acknowledgment(
            away_start_time=away_start,
            expected_return_time=expected_return,
            now=now_dt,
        )
        return ExtractedOvernightDeclaration(
            is_declaration=True,
            away_start_time=away_iso,
            expected_return_time=return_iso,
            confidence=confidence,
            is_one_off=True,
            source_text=source,
            notes=notes,
            acknowledgment=acknowledgment,
        )

    def is_declaration(self, text: str) -> bool:
        return self.extract(text).is_declaration

    def build_acknowledgment(
        self,
        *,
        away_start_time: datetime | None,
        expected_return_time: datetime | None,
        now: datetime | None = None,
    ) -> str:
        now_dt = _ensure_utc(now or datetime.now(timezone.utc))
        if expected_return_time is None:
            return "Understood. I’ll treat this as an overnight cycle and stay oriented around your return."

        return_phrase = _format_return_phrase(expected_return_time, now_dt)
        if away_start_time is not None and away_start_time > now_dt + timedelta(minutes=20):
            away_phrase = _format_time_short(away_start_time)
            return f"Understood. I’ll shift into overnight processing once you sign off around {away_phrase}, and I’ll expect you back {return_phrase}."
        return f"Alright. I’ll treat this as an overnight cycle and plan for you to be back {return_phrase}."

    def _extract_away_start(self, text: str, now: datetime, notes: dict[str, Any]) -> datetime | None:
        lower = text.lower()
        rel = re.search(r"\b(?:signing off|get(?:ting)? off|heading off|going offline|going to bed|turning in)\s+in\s+(\d{1,3})\s*(minutes?|mins?|hours?|hrs?)\b", lower)
        if rel:
            amount = int(rel.group(1))
            unit = rel.group(2)
            delta = timedelta(minutes=amount if unit.startswith("m") else amount * 60)
            notes["away_start_match"] = rel.group(0)
            notes["away_start_type"] = "relative"
            return now + delta

        for phrase in ("signing off", "heading off", "getting off", "going offline", "going to bed", "let's say", "lets say"):
            idx = lower.find(phrase)
            if idx < 0:
                continue
            excerpt = re.split(r"[.!?;]", text[idx: idx + 64], maxsplit=1)[0]
            direct = self._extract_first_time_fragment(excerpt, now, role="away", away_start=None, full_text=text)
            if direct is not None:
                notes["away_start_type"] = "explicit"
                notes["away_start_phrase"] = phrase
                return direct

        inferred = self._infer_from_two_time_sequence(text, now, role="away", away_start=None)
        if inferred is not None:
            notes["away_start_type"] = "inferred_pair"
            return inferred

        if any(token in lower for token in ("going to bed soon", "bed soon", "signing off soon", "heading off soon")):
            notes["away_start_type"] = "soon"
            return now + timedelta(minutes=self._soon_minutes)

        if self._strong_sleep_context(lower):
            notes["away_start_type"] = "contextual_now"
            return now + timedelta(minutes=self._soon_minutes)
        return None

    def _extract_return_time(
        self,
        text: str,
        now: datetime,
        away_start: datetime | None,
        notes: dict[str, Any],
    ) -> datetime | None:
        lower = text.lower()
        if "back in the morning" in lower or ("morning" in lower and "back" in lower and not re.search(r"\b\d{1,2}(?::\d{2})?", lower)):
            notes["return_type"] = "morning_phrase"
            base = away_start or now
            return self._default_morning_return(base)

        relative = re.search(r"\bback\s+in\s+(\d{1,2})\s*(hours?|hrs?)\b", lower)
        if relative:
            hours = int(relative.group(1))
            notes["return_type"] = "relative_hours"
            return (away_start or now) + timedelta(hours=hours)

        contextual = self._extract_contextual_time(text, now, role="return", away_start=away_start)
        if contextual is not None:
            notes["return_type"] = "explicit"
            return contextual

        if "until" in lower:
            until_match = re.search(r"\buntil\s+(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?|morning)\b", lower)
            if until_match:
                parsed = self._parse_time_fragment(until_match.group(1), now, role="return", away_start=away_start, full_text=text)
                if parsed is not None:
                    notes["return_type"] = "until"
                    return parsed
        return None

    def _extract_contextual_time(
        self,
        text: str,
        now: datetime,
        *,
        role: str,
        away_start: datetime | None = None,
    ) -> datetime | None:
        lower = text.lower()
        context_patterns = {
            "away": [
                r"(?:around|about|at|say|let's say|lets say)?\s*(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?(?:-?ish)?)\s*(?:tonight)?",
            ],
            "return": [
                r"(?:back\s+(?:around|on|by)\s+|until\s+)(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?(?:-?ish)?|morning)",
                r"(?:tomorrow\D{0,12}|morning\D{0,12})(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?(?:-?ish)?)",
            ],
        }
        anchors = _AWAY_HINTS if role == "away" else _RETURN_HINTS
        if not any(anchor in lower for anchor in anchors):
            if role == "away" and self._strong_sleep_context(lower):
                pass
            else:
                return self._infer_from_two_time_sequence(text, now, role=role, away_start=away_start)

        if role == "away":
            for phrase in ("signing off", "heading off", "getting off", "going to bed", "let's say", "lets say"):
                idx = lower.find(phrase)
                if idx >= 0:
                    candidate = text[idx: idx + 40]
                    parsed = self._extract_first_time_fragment(candidate, now, role=role, away_start=away_start, full_text=text)
                    if parsed is not None:
                        return parsed

        for pattern in context_patterns[role]:
            match = re.search(pattern, text, re.I)
            if not match:
                continue
            parsed = self._parse_time_fragment(match.group(1), now, role=role, away_start=away_start, full_text=text)
            if parsed is not None:
                return parsed

        return self._infer_from_two_time_sequence(text, now, role=role, away_start=away_start)

    def _infer_from_two_time_sequence(
        self,
        text: str,
        now: datetime,
        *,
        role: str,
        away_start: datetime | None,
    ) -> datetime | None:
        matches = list(re.finditer(r"\b(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?(?:-?ish)?)\b", text, re.I))
        if len(matches) < 2:
            return None
        ordered = [m.group(1) for m in matches[:2]]
        index = 0 if role == "away" else 1
        full_text = text.lower()
        fragment = ordered[index]
        return self._parse_time_fragment(fragment, now, role=role, away_start=away_start, full_text=full_text)

    def _extract_first_time_fragment(
        self,
        text: str,
        now: datetime,
        *,
        role: str,
        away_start: datetime | None,
        full_text: str,
    ) -> datetime | None:
        match = re.search(r"\b(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?(?:-?ish)?)\b", text, re.I)
        if not match:
            return None
        return self._parse_time_fragment(match.group(1), now, role=role, away_start=away_start, full_text=full_text)

    def _parse_time_fragment(
        self,
        fragment: str,
        now: datetime,
        *,
        role: str,
        away_start: datetime | None,
        full_text: str,
    ) -> datetime | None:
        frag = fragment.strip().lower().replace("-ish", "")
        if frag == "morning":
            return self._default_morning_return(away_start or now)

        has_am = "am" in frag or "a.m" in frag
        has_pm = "pm" in frag or "p.m" in frag
        date_anchor = self._date_anchor(full_text, role=role, now=now)

        if re.fullmatch(r"\d{1,2}(?::\d{2})?", frag) and not (has_am or has_pm):
            hour = int(frag.split(":", 1)[0])
            if role == "away":
                if "tonight" in full_text or self._strong_sleep_context(full_text):
                    frag = f"{frag} pm" if hour <= 11 else frag
                elif now.hour >= 15 and hour <= 11:
                    frag = f"{frag} pm"
            else:
                if any(token in full_text for token in ("tomorrow", "morning", "sleeping in", "wake")):
                    frag = f"{frag} am" if hour <= 11 else frag
                elif away_start is not None and hour <= away_start.hour:
                    frag = f"{frag} am" if hour <= 11 else frag
                else:
                    frag = f"{frag} am" if hour <= 11 else frag

        default_date = date_anchor.date() if date_anchor is not None else now.date()
        try:
            parsed = dtparser.parse(
                frag,
                default=datetime.combine(default_date, datetime.min.time(), tzinfo=timezone.utc),
                fuzzy=True,
            )
        except (ValueError, TypeError, OverflowError):
            return None
        parsed = _ensure_utc(parsed)
        if date_anchor is not None:
            parsed = parsed.replace(year=date_anchor.year, month=date_anchor.month, day=date_anchor.day)
        if role == "return" and away_start is not None and parsed <= away_start:
            parsed = self._repair_return_after_away(parsed, away_start, full_text, {})
        return parsed

    def _repair_return_after_away(
        self,
        parsed: datetime,
        away_start: datetime,
        full_text: str,
        notes: dict[str, Any],
    ) -> datetime:
        repaired = parsed
        if repaired <= away_start:
            repaired = repaired + timedelta(days=1)
            notes["return_rollover"] = True
        if "morning" in full_text and repaired.hour >= 12:
            repaired = repaired.replace(hour=max(repaired.hour - 12, 0))
            if repaired <= away_start:
                repaired = repaired + timedelta(days=1)
        return repaired

    def _date_anchor(self, full_text: str, *, role: str, now: datetime) -> datetime | None:
        if role == "return" and any(token in full_text for token in ("tomorrow", "in the morning", "morning", "sleeping in")):
            return now + timedelta(days=1)
        if role == "away" and "tomorrow" in full_text and "tonight" not in full_text:
            return now + timedelta(days=1)
        return None

    def _default_morning_return(self, base: datetime) -> datetime:
        hour = int(self._morning_hour)
        minute = int(round((self._morning_hour - hour) * 60))
        target = _ensure_utc(base).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= base:
            target += timedelta(days=1)
        return target

    def _strong_sleep_context(self, lowered: str) -> bool:
        return any(token in lowered for token in ("going to bed", "bed soon", "getting tired", "sleeping in", "in the morning"))

    def _llm_fallback(
        self,
        text: str,
        now: datetime,
        *,
        topology=None,
        cfg: dict | None = None,
    ) -> dict[str, Any] | None:
        if topology is None:
            return None
        try:
            endpoint = topology.primary_endpoint()
        except Exception:
            return None
        qcfg = (cfg or {}).get("qwen3", {})
        prompt = (
            "Return JSON only with keys is_declaration, away_start_time, expected_return_time, confidence. "
            "Interpret the user's message as a possible overnight availability declaration. "
            f"Reference time: {_iso(now)}. User message: {text!r}"
        )
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(
                    f"{endpoint}/v1/chat/completions",
                    json={
                        "model": "qwen3",
                        "messages": [
                            {"role": "system", "content": "You extract structured overnight availability. Output JSON only."},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.0,
                        "max_tokens": min(int(qcfg.get("max_tokens", 256)), 256),
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"].strip()
                if content.startswith("```"):
                    content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.I | re.M).strip()
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    return None
                return parsed
        except Exception:
            return None


def _format_time_short(dt: datetime) -> str:
    label = dt.astimezone(timezone.utc).strftime("%I:%M %p")
    return label.lstrip("0").replace(":00", "")


def _format_return_phrase(dt: datetime, now: datetime) -> str:
    target = dt.astimezone(timezone.utc)
    delta_days = (target.date() - now.date()).days
    prefix = "around " + _format_time_short(target)
    if delta_days <= 0:
        return prefix
    if delta_days == 1:
        return f"tomorrow around {_format_time_short(target)}"
    return f"on {target.strftime('%A')} around {_format_time_short(target)}"


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return _ensure_utc(dt).isoformat().replace("+00:00", "Z")


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except Exception:
        return None
