from __future__ import annotations

from datetime import datetime, timezone

from runtime.overnight_declaration import OvernightDeclarationExtractor


NOW = datetime(2026, 3, 23, 21, 0, tzinfo=timezone.utc)


def test_extracts_heading_off_and_return_tomorrow():
    extractor = OvernightDeclarationExtractor({"overnight_cycle": {"enabled": True}})

    result = extractor.extract("I'm heading off. I'll be back around 9 tomorrow.", now=NOW)

    assert result.is_declaration is True
    assert result.away_start_time is not None
    assert result.expected_return_time is not None
    assert result.confidence >= 0.75
    assert "overnight" in (result.acknowledgment or "").lower()


def test_extracts_relative_signoff_time():
    extractor = OvernightDeclarationExtractor({"overnight_cycle": {"enabled": True}})

    result = extractor.extract("I'm signing off in 20 minutes. I'll be back around 9 AM.", now=NOW)

    assert result.is_declaration is True
    assert result.away_start_time == "2026-03-23T21:20:00Z"
    assert result.expected_return_time == "2026-03-24T09:00:00Z"


def test_extracts_fuzzy_sleeping_in_phrase():
    extractor = OvernightDeclarationExtractor({"overnight_cycle": {"enabled": True}})

    result = extractor.extract(
        "Tomorrow my kids don't have school, so I'm probably sleeping in until 9.",
        now=NOW,
    )

    assert result.is_declaration is True
    assert result.expected_return_time == "2026-03-24T09:00:00Z"
    assert result.confidence >= 0.6


def test_rejects_non_declaration_offline_system_message():
    extractor = OvernightDeclarationExtractor({"overnight_cycle": {"enabled": True}})

    result = extractor.extract("The server went offline around 10 and came back at 11.", now=NOW)

    assert result.is_declaration is False


def test_extracts_two_time_sequence_with_sleep_context():
    extractor = OvernightDeclarationExtractor({"overnight_cycle": {"enabled": True}})

    result = extractor.extract("I'm getting tired. Let's say 10 tonight and 9 tomorrow.", now=NOW)

    assert result.is_declaration is True
    assert result.away_start_time == "2026-03-23T22:00:00Z"
    assert result.expected_return_time == "2026-03-24T09:00:00Z"
