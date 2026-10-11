from datetime import UTC, datetime

import pytest

from avito_personal_mcp.listing_date import SARATOV, parse_listing_date

REFERENCE = datetime(2026, 10, 6, 22, 30, tzinfo=SARATOV)


@pytest.mark.parametrize(
    ("text", "iso", "precision"),
    [
        (" · вчера в 09:15", "2026-10-05T09:15:00+04:00", "minute"),
        ("\u00a0·\u00a0сегодня в 00:05", "2026-10-06T00:05:00+04:00", "minute"),
        ("позавчера в 23:59", "2026-10-04T23:59:00+04:00", "minute"),
        ("3 дня назад", "2026-10-03", "day"),
        ("1 день назад", "2026-10-05", "day"),
        ("5 октября 2026 в 09:15", "2026-10-05T09:15:00+04:00", "minute"),
        ("5 октября, 09:15", "2026-10-05T09:15:00+04:00", "minute"),
        ("5 октября", "2026-10-05", "day"),
        ("сегодня", "2026-10-06", "day"),
    ],
)
def test_date_labels_resolve_against_fixed_saratov_reference(text, iso, precision):
    result = parse_listing_date(text, REFERENCE)
    assert result["date_iso"] == iso
    assert result["date_precision"] == precision
    assert result["date_parse_status"] == "resolved"
    assert result["date_observed_at"] == "2026-10-06T22:30:00+04:00"


def test_utc_day_boundary_uses_saratov_calendar_day():
    reading_time = datetime(2026, 10, 5, 21, 30, tzinfo=UTC)
    result = parse_listing_date("вчера в 09:15", reading_time)
    assert result["date_iso"] == "2026-10-05T09:15:00+04:00"
    assert result["date_observed_at"] == "2026-10-06T01:30:00+04:00"


def test_saved_relative_label_does_not_change_when_read_later():
    first = parse_listing_date("вчера в 09:15", REFERENCE)
    saved_reference = datetime.fromisoformat(first["date_observed_at"])
    assert parse_listing_date(first["date"], saved_reference) == first


def test_year_boundary_and_leap_day_are_explicitly_inferred():
    january = datetime(2026, 1, 2, tzinfo=SARATOV)
    result = parse_listing_date("31 декабря в 23:50", january)
    assert result["date_iso"] == "2025-12-31T23:50:00+04:00"
    assert result["date_year_inferred"] is True
    leap = parse_listing_date("29 февраля", REFERENCE)
    assert leap["date_iso"] == "2024-02-29"
    assert leap["date_year_inferred"] is True


@pytest.mark.parametrize(
    "text",
    [
        "вчера в 25:00",
        "31 февраля 2026",
        "недавно",
        "вчера в 09:15 · 143 просмотра",
        "999999999 дней назад",
    ],
)
def test_unknown_or_invalid_labels_preserve_text_without_inventing_timestamp(text):
    result = parse_listing_date(text, REFERENCE)
    assert result["date"] == text
    assert result["date_iso"] is None
    assert result["date_parse_status"] == "unresolved"


@pytest.mark.parametrize("text", [None, "", " · "])
def test_missing_date_is_explicit(text):
    result = parse_listing_date(text, REFERENCE)
    assert result["date"] is None
    assert result["date_iso"] is None
    assert result["date_parse_status"] == "missing"


def test_naive_clock_is_rejected_instead_of_using_host_timezone():
    with pytest.raises(ValueError, match="timezone"):
        parse_listing_date("вчера в 09:15", datetime(2026, 10, 6, 22, 30))
