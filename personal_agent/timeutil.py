"""Timezone-aware, forgiving natural-language time parsing.

All timestamps are persisted as local wall-clock ISO strings
(``YYYY-MM-DDTHH:MM``) because Personal Agent is a single-user, single-timezone
tool. That keeps stored data human-readable and free of offset drift.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Kolkata"

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_REL_UNITS = {
    "min": 1, "mins": 1, "minute": 1, "minutes": 1, "m": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "d": 1440, "day": 1440, "days": 1440,
    "w": 10080, "week": 10080, "weeks": 10080,
}

FMT = "%Y-%m-%dT%H:%M"


class TimeZone:
    """Small holder so the active zone can be swapped at runtime."""

    def __init__(self, name: str = DEFAULT_TZ):
        self.name = name
        self._zone: ZoneInfo = _safe_zone(name)

    def use(self, name: str) -> None:
        self.name = name
        self._zone = _safe_zone(name)

    @property
    def zone(self) -> ZoneInfo:
        return self._zone

    def now(self) -> datetime:
        """Current local time as a NAIVE datetime.

        Deliberately naive: every timestamp is stored as local wall-clock
        (see module docstring), so mixing aware values would introduce offset
        bugs for no benefit. `now_utc()` exists if an aware value is needed.
        """
        return datetime.now(self._zone).replace(tzinfo=None)

    def now_utc(self) -> datetime:
        return datetime.now(self._zone)

    def today(self) -> date:
        return self.now().date()

    def offset_label(self) -> str:
        offset = self.now_utc().utcoffset() or timedelta(0)
        total = int(offset.total_seconds()) // 60
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        return f"GMT{sign}{total // 60}:{total % 60:02d}"


def _safe_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - only on a machine with bad tzdata
        return ZoneInfo(DEFAULT_TZ)


TZ = TimeZone()


# --------------------------------------------------------------------------- #
# parse / format
# --------------------------------------------------------------------------- #
def to_iso(dt: datetime | None) -> str | None:
    return dt.strftime(FMT) if dt else None


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    value = str(value).strip().replace(" ", "T")
    for fmt in (FMT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except ValueError:
        return None


def parse_duration(text: str | None) -> int | None:
    """'90m' / '1.5h' / '2 hours' / '45' (bare number => minutes)."""
    if not text:
        return None
    text = str(text).strip().lower()
    match = re.search(r"(\d+(?:\.\d+)?)\s*([a-z]*)", text)
    if not match:
        return None
    amount, unit = float(match.group(1)), match.group(2)
    if not unit:
        return int(amount)
    unit = unit.rstrip("s")
    if unit in _REL_UNITS:
        return int(amount * _REL_UNITS[unit])
    if unit == "month" or unit == "months":
        return int(amount * 30 * 1440)
    if unit == "year" or unit == "years":
        return int(amount * 365 * 1440)
    return None


def parse_when(text: str | None, base: datetime | None = None) -> datetime | None:
    """Best-effort natural-language datetime parser.

    Understands ISO strings, 'today', 'tomorrow', weekday names, 'next mon',
    '3pm', '15:30', '26 sep', '2026-09-26 15:30', 'in 2 hours', 'eod',
    'tonight', 'this weekend' and combinations like 'tomorrow 9am'.
    """
    if not text:
        return None
    raw = str(text).strip().lower()
    if raw in {"now", "right now"}:
        return base or TZ.now().replace(second=0, microsecond=0)

    ref = (base or TZ.now()).replace(second=0, microsecond=0)
    has_clock = False
    hour: int | None = None
    minute = 0
    day_offset: int | None = None
    target_date: date | None = None
    matched_any = False

    # --- explicit ISO ----------------------------------------------------- #
    iso = from_iso(text)
    if iso and re.search(r"\d{4}-\d{2}-\d{2}", raw):
        return iso

    # --- in N units ------------------------------------------------------- #
    match = re.search(r"\bin\s+(\d+(?:\.\d+)?)\s*(min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)\b", raw)
    if match:
        minutes = parse_duration(match.group(1) + match.group(2))
        if minutes is not None:
            return ref + timedelta(minutes=minutes)

    # --- clock time ------------------------------------------------------- #
    # "14:30" / "9:45pm" (colon present) or "5pm" / "5 pm" (meridiem required).
    # A bare number is NOT a clock time — that would swallow the day in "26 sep".
    match = re.search(r"\b(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)?\b", raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        meridiem = (match.group(3) or "").replace(".", "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            has_clock, matched_any = True, True
        else:
            hour, minute = None, 0

    if hour is None:
        match = re.search(r"\b(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)\b", raw)
        if match:
            hh = int(match.group(1))
            meridiem = match.group(2).replace(".", "").lower()
            if meridiem == "pm" and hh < 12:
                hh += 12
            elif meridiem == "am" and hh == 12:
                hh = 0
            if 0 <= hh <= 23:
                hour, minute, has_clock, matched_any = hh, 0, True, True

    # --- relative days ---------------------------------------------------- #
    # Order matters: the longer phrases must be tested first, otherwise
    # "day after tomorrow" would match plain "tomorrow" (+1) and be wrong.
    if re.search(r"\bday after tomorrow\b", raw):
        day_offset, matched_any = 2, True
    elif re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", raw):
        day_offset, matched_any = 0, True
    elif re.search(r"\b(tomorrow|tmrw|tmr)\b", raw):
        day_offset, matched_any = 1, True
    elif re.search(r"\byesterday\b", raw):
        day_offset, matched_any = -1, True
    elif re.search(r"\bthis weekend\b", raw):
        day_offset = (5 - ref.weekday()) % 7 or 7
        matched_any = True

    # --- weekday ---------------------------------------------------------- #
    match = re.search(r"\b(next\s+|this\s+)?(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(day|s|nesday|rsday|urday)?\b", raw)
    if match:
        prefix = (match.group(1) or "").strip()
        target_wd = WEEKDAYS[match.group(2)]
        ahead = (target_wd - ref.weekday()) % 7
        if ahead == 0:
            # "friday" on a Friday means next Friday — but "friday 3pm" means
            # today at 3pm, which is what makes "sunday 8am to sunday 12pm"
            # a four-hour block rather than a seven-day one.
            ahead = 7 if (prefix == "next" or not has_clock) else 0
        elif prefix == "next":
            ahead += 7
        day_offset = ahead if day_offset is None else day_offset
        matched_any = True

    # --- explicit date ---------------------------------------------------- #
    # "26 sep", "26th september", "26 sep 2027" — day before month.
    match = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
        r"(?:\s+(\d{4}))?\b", raw)
    if match and match.group(2) in MONTHS:
        day, month = int(match.group(1)), MONTHS[match.group(2)]
        year = int(match.group(3)) if match.group(3) else ref.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            candidate = None
        if candidate:
            if not match.group(3) and candidate < ref.date():
                candidate = date(year + 1, month, day)
            target_date, matched_any = candidate, True

    # "sep 26", "september 26th" — month before day. Only when no day-month
    # match already fired, otherwise "26 sep 14:00" would degrade to "sep 26".
    if target_date is None:
        match = re.search(
            r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", raw)
        if match and match.group(1) in MONTHS:
            month, day = MONTHS[match.group(1)], int(match.group(2))
            try:
                candidate = date(ref.year, month, day)
            except ValueError:
                candidate = None
            if candidate:
                if candidate < ref.date():
                    candidate = date(ref.year + 1, month, day)
                target_date, matched_any = candidate, True

    # --- multi-day shorthands --------------------------------------------- #
    if target_date is None and day_offset is None:
        if re.search(r"\bnext week\b", raw):
            day_offset, matched_any = 7, True
        elif re.search(r"\bnext month\b", raw):
            day_offset, matched_any = 30, True

    # --- named day-parts --------------------------------------------------- #
    if not has_clock:
        if re.search(r"\bnoon\b|\bmidday\b", raw):
            hour, minute, has_clock, matched_any = 12, 0, True, True
        elif re.search(r"\b(midnight)\b", raw):
            hour, minute, has_clock, matched_any = 0, 0, True, True
        elif re.search(r"\b(eod|end of day)\b", raw):
            hour, minute, has_clock, matched_any = 18, 0, True, True
        elif re.search(r"\b(tonight)\b", raw):
            hour, minute, has_clock, matched_any = 19, 0, True, True
        elif re.search(r"\b(morning)\b", raw):
            hour, minute, has_clock, matched_any = 9, 0, True, True
        elif re.search(r"\b(afternoon)\b", raw):
            hour, minute, has_clock, matched_any = 14, 0, True, True
        elif re.search(r"\b(evening)\b", raw):
            hour, minute, has_clock, matched_any = 19, 0, True, True

    if not matched_any:
        return None

    # A bare clock time with no date means "today if still ahead, else tomorrow".
    if target_date is None and day_offset is None and has_clock:
        result = ref.replace(hour=hour, minute=minute)
        return result if result >= ref else result + timedelta(days=1)

    if target_date is not None:
        base_day = datetime.combine(target_date, datetime.min.time())
        if has_clock:
            return base_day.replace(hour=hour, minute=minute)
        if target_date == ref.date():
            return ref
        return base_day.replace(hour=9, minute=0)

    result = ref + timedelta(days=day_offset)
    if has_clock:
        return result.replace(hour=hour, minute=minute)
    return result.replace(hour=9, minute=0) if day_offset else ref


def parse_span(text: str | None, start: datetime) -> datetime | None:
    """End time from a duration or a 'to <time>' style string."""
    if not text:
        return None
    match = re.search(r"\b(?:to|until|till|[-–])\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", str(text).lower())
    if match:
        end = parse_when(match.group(1), base=start)
        if end and end <= start:
            end += timedelta(days=1)
        return end
    minutes = parse_duration(text)
    return start + timedelta(minutes=minutes) if minutes else None


# --------------------------------------------------------------------------- #
# humanising
# --------------------------------------------------------------------------- #
def _as_dt(value) -> datetime | None:
    """Accept a datetime, a date, or an ISO string."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return from_iso(value)
    return None


def fmt_date(value: date | datetime | str | None) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%a %d %b")
    if isinstance(value, date):
        return value.strftime("%a %d %b")
    parsed = from_iso(str(value))
    return parsed.strftime("%a %d %b") if parsed else str(value)


def fmt_time(value: datetime | str | None) -> str:
    dt = _as_dt(value)
    return dt.strftime("%I:%M %p").lstrip("0") if dt else ""


def fmt_range(start, end) -> str:
    start, end = _as_dt(start), _as_dt(end)
    if not start:
        return "—"
    if not end:
        return fmt_time(start)
    if start.date() == end.date():
        return f"{fmt_time(start)} – {fmt_time(end)}"
    return f"{fmt_date(start)} {fmt_time(start)} – {fmt_date(end)} {fmt_time(end)}"


def humanize(value, ref: datetime | None = None) -> str:
    """'now' / 'in 5 min' / 'in 2h' / 'tomorrow' / '3d ago' / 'on Tue 14 Sep'."""
    value = _as_dt(value)
    if not value:
        return ""
    ref = _as_dt(ref) or TZ.now()
    secs = (value - ref).total_seconds()
    past = secs < 0
    # Anything under a minute either way reads as "now" — this also stops a
    # task saved "just now" from displaying as "1 min ago" due to clock skew
    # between two separate TZ.now() calls.
    if abs(secs) < 60:
        return "now"
    mins = int(round(abs(secs) / 60))
    if mins < 60:
        return f"{mins} min ago" if past else f"in {mins} min"
    hours = mins // 60
    days = (value.date() - ref.date()).days
    if hours < 24 and value.date() == ref.date():
        return f"{hours}h ago" if past else f"in {hours}h"
    if days == 0:
        return f"{hours}h ago" if past else f"in {hours}h"
    if days == 1:
        return "yesterday" if past else "tomorrow"
    if days == -1:
        return "yesterday"
    if 1 < days <= 6:
        return f"in {days}d"
    if -6 <= days < -1:
        return f"{-days}d ago"
    return f"on {fmt_date(value)}"


def day_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, datetime.min.time()),
        datetime.combine(day, datetime.max.time()).replace(microsecond=0),
    )


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end
