"""Deterministic, bounded parsing for common Event date/time expressions."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

MONTHS = {
    name.casefold(): index
    for index, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
}
MONTH_ALIASES = {key[:3]: value for key, value in MONTHS.items()}
ZONE_NAMES = {
    "mountain": "America/Denver",
    "mountain time": "America/Denver",
    "pacific": "America/Los_Angeles",
    "pacific time": "America/Los_Angeles",
    "central": "America/Chicago",
    "central time": "America/Chicago",
    "eastern": "America/New_York",
    "eastern time": "America/New_York",
    "utc": "UTC",
}
ZONE_RE = re.compile(
    r"\b(?:America/[A-Za-z_]+(?:/[A-Za-z_]+)?|UTC|"
    r"(?:Mountain|Pacific|Central|Eastern)(?:\s+Time)?)\b",
    re.IGNORECASE,
)
MONTH_DATE_RE = re.compile(
    r"\b(?P<month>January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+"
    r"(?P<day>\d{1,2})(?:,?\s+(?P<year>\d{2}|20\d{2}))?\b",
    re.IGNORECASE,
)
US_NUMERIC_DATE_RE = re.compile(
    r"\b(?P<month>\d{1,2})[/-](?P<day>\d{1,2})[/-](?P<year>\d{2}|20\d{2})\b"
)
US_SHORT_DATE_RE = re.compile(r"\b(?P<month>\d{1,2})[/-](?P<day>\d{1,2})\b")
ISO_DATE_RE = re.compile(r"\b(?P<year>20\d{2})-(?P<month>\d{2})-(?P<day>\d{2})\b")
TIME_TOKEN = (
    r"(?:noon|midnight|(?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*[ap]\.?m\.?|"
    r"(?:2[0-3]|[01]?\d):[0-5]\d|(?:1[0-2]|0?[1-9])(?::[0-5]\d)?)"
)
TIME_RE = re.compile(
    rf"\b(?P<start>{TIME_TOKEN})(?:\s*(?:to|through|[-–])\s*(?P<end>{TIME_TOKEN}))?\b",
    re.IGNORECASE,
)


def _zone(text: str, default_timezone: str) -> tuple[str, bool]:
    match = ZONE_RE.search(text)
    if not match:
        return default_timezone, False
    value = match.group(0).strip()
    canonical = ZONE_NAMES.get(value.casefold(), value)
    ZoneInfo(canonical)
    return canonical, True


def _date_parts(text: str):
    match = ISO_DATE_RE.search(text)
    if match:
        return int(match["year"]), int(match["month"]), int(match["day"]), False, False
    match = US_NUMERIC_DATE_RE.search(text)
    if match:
        year = int(match["year"])
        if year < 100:
            year += 2000
        ambiguous = int(match["month"]) <= 12 and int(match["day"]) <= 12
        return year, int(match["month"]), int(match["day"]), False, ambiguous
    match = US_SHORT_DATE_RE.search(text)
    if match:
        ambiguous = int(match["month"]) <= 12 and int(match["day"]) <= 12
        return None, int(match["month"]), int(match["day"]), True, ambiguous
    match = MONTH_DATE_RE.search(text)
    if not match:
        return None
    month_name = match["month"].rstrip(".").casefold()
    month = MONTHS.get(month_name, MONTH_ALIASES.get(month_name[:3]))
    year = int(match["year"]) if match["year"] else None
    if year is not None and year < 100:
        year += 2000
    return year, month, int(match["day"]), year is None, False


def _date_end(text: str) -> int:
    for pattern in (ISO_DATE_RE, US_NUMERIC_DATE_RE, US_SHORT_DATE_RE, MONTH_DATE_RE):
        match = pattern.search(text)
        if match:
            return match.end()
    return 0


def _time_value(raw: str, inherited_meridiem: str | None = None):
    value = re.sub(r"\s+", "", raw.casefold()).replace(".", "")
    if value == "noon":
        return (12, 0), False
    if value == "midnight":
        return (0, 0), False
    meridiem = "p" if value.endswith("pm") else "a" if value.endswith("am") else inherited_meridiem
    value = re.sub(r"(?:am|pm)$", "", value)
    hour_text, colon, minute_text = value.partition(":")
    hour = int(hour_text)
    minute = int(minute_text) if colon else 0
    if meridiem:
        if not 1 <= hour <= 12:
            return None, False
        hour = hour % 12 + (12 if meridiem == "p" else 0)
        return (hour, minute), False
    if colon and hour > 12:
        return (hour, minute), False
    if hour == 12:
        return (12, minute), True
    if 1 <= hour <= 11:
        return None, True
    return (hour, minute), False


def _times(text: str, offset: int = 0):
    match = TIME_RE.search(text, offset)
    if not match:
        return None, None, False
    end_raw = match["end"]
    end_has_meridiem = bool(end_raw and re.search(r"[ap]\.?m\.?$", end_raw, re.IGNORECASE))
    inherited = (
        "p"
        if end_has_meridiem and re.search(r"p\.?m\.?$", end_raw, re.IGNORECASE)
        else "a"
        if end_has_meridiem
        else None
    )
    start, start_ambiguous = _time_value(match["start"], inherited)
    end, end_ambiguous = _time_value(end_raw, inherited) if end_raw else (None, False)
    return start, end, start_ambiguous or end_ambiguous


def _infer_year(year: int | None, month: int, day: int, zone_name: str, today: date | None):
    if year is not None:
        return year, False, date(year, month, day)
    local_today = today or datetime.now(ZoneInfo(zone_name)).date()
    # A date earlier in the current month may be a recently passed event. Keep it
    # in this year and warn; earlier calendar months roll to the next year.
    inferred = local_today.year + (1 if month < local_today.month else 0)
    value = date(inferred, month, day)
    return inferred, True, value


def parse_source_schedule(text: str, default_timezone: str, today: date | None = None):
    """Return an internal schedule suggestion, preserving components we can parse."""
    if not isinstance(text, str) or not text.strip():
        return None
    zone_name, explicit_zone = _zone(text, default_timezone)
    parsed_date = _date_parts(text)
    start_time, end_time, time_ambiguous = _times(text, _date_end(text))
    if parsed_date is None and start_time is None:
        return None

    date_only = None
    year_inferred = False
    past_date = False
    numeric_ambiguous = False
    if parsed_date is not None:
        year, month, day, missing_year, numeric_ambiguous = parsed_date
        try:
            year, year_inferred, resolved_date = _infer_year(year, month, day, zone_name, today)
        except ValueError:
            return None
        date_only = resolved_date.isoformat()
        if start_time:
            start_dt = datetime(year, month, day, *start_time)
            local_start = start_dt.isoformat()
            local_end = None
            if end_time:
                end_dt = datetime(year, month, day, *end_time)
                if end_dt <= start_dt:
                    end_dt += timedelta(days=1)
                local_end = end_dt.isoformat()
        else:
            local_start = local_end = None
        local_today = today or datetime.now(ZoneInfo(zone_name)).date()
        past_date = resolved_date < local_today
    else:
        local_start = local_end = None

    reasons = []
    if year_inferred:
        reasons.append(f"I suggested {resolved_date.year} for the year; please confirm it.")
    if past_date:
        reasons.append("This date has passed. Check whether the year is correct.")
    if numeric_ambiguous:
        reasons.append("I read this numeric date using the U.S. month/day convention.")
    if time_ambiguous:
        reasons.append("Please confirm AM or PM for the time.")
    if explicit_zone and zone_name != default_timezone:
        reasons.append(f"The source says {zone_name}; please confirm the time zone.")
    return {
        "local_start": local_start,
        "timezone": zone_name,
        "local_end": local_end,
        "date_only": date_only,
        "start_time": f"{start_time[0]:02d}:{start_time[1]:02d}" if start_time else None,
        "end_time": f"{end_time[0]:02d}:{end_time[1]:02d}" if end_time else None,
        "year_inferred": year_inferred,
        "past_date": past_date,
        "interpretation_note": " ".join(reasons) or None,
    }


def parse_schedule_answer(answer: str, default_timezone: str):
    """Parse a natural-language answer or the UI's delimited local picker values."""
    raw = answer.strip()
    if not raw:
        raise ValueError("schedule answer required")
    if raw.count("|") >= 1:
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) != 3:
            raise ValueError("invalid structured schedule")
        start, end, zone = parts
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}", start):
            raise ValueError("invalid start")
        if end and not re.fullmatch(r"20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}", end):
            raise ValueError("invalid end")
        ZoneInfo(zone)
        return {
            "local_start": datetime.fromisoformat(start).isoformat(),
            "local_end": datetime.fromisoformat(end).isoformat() if end else None,
            "timezone": zone,
            "date_only": start[:10],
            "start_time": start[11:],
            "end_time": end[11:] if end else None,
        }

    zone_name, explicit_zone = _zone(raw, default_timezone)
    cleaned = ZONE_RE.sub(" ", raw)
    # ISO local date-time is already unambiguous. Preserve optional end date/time.
    iso_matches = re.findall(r"\b20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}\b", cleaned)
    if iso_matches:
        start = datetime.fromisoformat(iso_matches[0])
        end = datetime.fromisoformat(iso_matches[1]) if len(iso_matches) > 1 else None
        if end and end <= start:
            raise ValueError("end must follow start")
        return {
            "local_start": start.isoformat(),
            "local_end": end.isoformat() if end else None,
            "timezone": zone_name,
            "date_only": start.date().isoformat(),
            "start_time": start.strftime("%H:%M"),
            "end_time": end.strftime("%H:%M") if end else None,
        }

    parsed_date = _date_parts(cleaned)
    start_time, end_time, time_ambiguous = _times(cleaned, _date_end(cleaned))
    if parsed_date is None or start_time is None or time_ambiguous:
        raise ValueError("complete date and unambiguous start time required")
    year, month, day, _, _ = parsed_date
    year, _, date_value = _infer_year(year, month, day, zone_name, None)
    start = datetime(year, month, day, *start_time)
    end = datetime(year, month, day, *end_time) if end_time else None
    if end and end <= start:
        end += timedelta(days=1)
    return {
        "local_start": start.isoformat(),
        "local_end": end.isoformat() if end else None,
        "timezone": zone_name,
        "date_only": date_value.isoformat(),
        "start_time": start.strftime("%H:%M"),
        "end_time": end.strftime("%H:%M") if end else None,
    }
