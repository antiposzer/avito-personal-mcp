"""Resolve rendered Russian date labels against their reading time in Saratov."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TIMEZONE_NAME = "Europe/Saratov"
SARATOV = ZoneInfo(TIMEZONE_NAME)
MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


def now_in_saratov() -> datetime:
    return datetime.now(SARATOV)


def parse_listing_date(raw: str | None, observed_at: datetime | None = None) -> dict:
    """Keep the original label; leave unsupported or invalid dates unresolved."""

    reference = observed_at if observed_at is not None else now_in_saratov()
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("Date reading time must include a timezone")
    reference = reference.astimezone(SARATOV)
    text = " ".join(raw.split()).lstrip("·• ") if isinstance(raw, str) else ""
    result = {
        "date": text or None,
        "date_iso": None,
        "date_precision": None,
        "date_observed_at": reference.isoformat(timespec="seconds"),
        "date_timezone": TIMEZONE_NAME,
        "date_year_inferred": False,
        "date_parse_status": "unresolved" if text else "missing",
    }
    if not text:
        return result
    label = text.casefold()
    value: date | None = None
    hours = minutes = None
    inferred = False
    try:
        relative = re.fullmatch(r"(сегодня|вчера|позавчера)(?:\s+в\s+(\d{1,2}):(\d{2}))?", label)
        days_ago = re.fullmatch(r"(\d+)\s+(?:день|дня|дней)\s+назад", label)
        calendar = re.fullmatch(
            r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")"
            r"(?:\s+(\d{4}))?(?:(?:\s*(?:в|,)\s*|\s+)(\d{1,2}):(\d{2}))?",
            label,
        )
        if relative:
            value = reference.date() - timedelta(
                days={"сегодня": 0, "вчера": 1, "позавчера": 2}[relative[1]]
            )
            if relative[2] is not None:
                hours, minutes = int(relative[2]), int(relative[3])
        elif days_ago:
            value = reference.date() - timedelta(days=int(days_ago[1]))
        elif calendar:
            day, month = int(calendar[1]), MONTHS[calendar[2]]
            if calendar[3] is not None:
                value = date(int(calendar[3]), month, day)
            else:
                # Avito omits years in some labels. Use the most recent past
                # occurrence, and explicitly record that the year was inferred.
                inferred = True
                for year in range(reference.year, reference.year - 5, -1):
                    try:
                        candidate = date(year, month, day)
                    except ValueError:
                        continue
                    if candidate <= reference.date():
                        value = candidate
                        break
            if calendar[4] is not None:
                hours, minutes = int(calendar[4]), int(calendar[5])
        if value is None:
            return result
        if hours is not None:
            timestamp = datetime(value.year, value.month, value.day, hours, minutes, tzinfo=SARATOV)
            result["date_iso"] = timestamp.isoformat(timespec="seconds")
            result["date_precision"] = "minute"
        else:
            result["date_iso"] = value.isoformat()
            result["date_precision"] = "day"
    except (ValueError, OverflowError):
        return result
    result["date_year_inferred"] = inferred
    result["date_parse_status"] = "resolved"
    return result
