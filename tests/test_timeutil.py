"""Time-parsing regression tests. Run: python -m pytest tests -q  (or: python tests/test_timeutil.py)"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from personal_agent.timeutil import (  # noqa: E402
    TZ,
    from_iso,
    humanize,
    parse_duration,
    parse_span,
    parse_when,
)

TODAY = TZ.today()
REF = datetime.combine(TODAY, datetime.min.time()).replace(hour=9)


def d(offset: int, hour: int = 9, minute: int = 0) -> datetime:
    return datetime.combine(TODAY + timedelta(days=offset), datetime.min.time()).replace(
        hour=hour, minute=minute)


CASES = [
    # (input, expected)
    ("today", d(0)),
    ("tomorrow", d(1)),
    ("tomorrow 7am", d(1, 7)),
    ("tomorrow 7 pm", d(1, 19)),
    ("4pm tomorrow", d(1, 16)),
    ("tonight", d(0, 19)),
    ("yesterday", d(-1)),
    ("day after tomorrow", d(2)),
    ("14:30", d(0, 14, 30)),
    ("9am", d(0, 9)),
    ("9:45pm", d(0, 21, 45)),
    ("noon", d(0, 12)),
    ("eod", d(0, 18)),
    ("26 sep 14:00", datetime(TODAY.year, 9, 26, 14, 0)),
    ("2026-09-26 15:30", datetime(2026, 9, 26, 15, 30)),
    ("in 2 hours", d(0, 11)),
    ("in 3 days", d(3)),
]


def main() -> int:
    failures = []

    for text, expected in CASES:
        got = parse_when(text, base=REF)
        if got != expected:
            failures.append(f"parse_when({text!r})\n     got {got}\n  wanted {expected}")

    # Weekday handling is relative to the reference, so assert structurally.
    fri = parse_when("friday 5pm", base=REF)
    if not fri or fri.weekday() != 4 or fri.hour != 17:
        failures.append(f"parse_when('friday 5pm') -> {fri}, wanted a Friday 17:00")
    mon = parse_when("monday", base=REF)
    if not mon or mon.weekday() != 0:
        failures.append(f"parse_when('monday') -> {mon}, wanted a Monday")

    # These must NOT be read as dates.
    for text in ("add task submit assignment", "task", "buy lab goggles", "pay hostel fee"):
        if parse_when(text) is not None:
            failures.append(f"parse_when({text!r}) should return None, got {parse_when(text)}")

    # A weekday WITH a clock time, resolved against a base already on that
    # weekday, must stay on the same day. "sunday 8am to sunday 12pm" used to
    # jump a week forward and create a seven-day event.
    sunday_morning = datetime(2026, 9, 27, 8, 0)
    got = parse_when("Sunday 12pm", base=sunday_morning)
    if got != datetime(2026, 9, 27, 12, 0):
        failures.append(f"parse_when('Sunday 12pm', base=Sun 08:00) -> {got}, "
                        "wanted 2026-09-27 12:00 (same day, not next week)")
    got = parse_when("12pm", base=sunday_morning)
    if got != datetime(2026, 9, 27, 12, 0):
        failures.append(f"parse_when('12pm', base=Sun 08:00) -> {got}, wanted 2026-09-27 12:00")
    # A bare weekday with no clock still means the NEXT one, even from that day.
    got = parse_when("friday", base=sunday_morning)
    if got != datetime(2026, 10, 2, 9, 0):
        failures.append(f"parse_when('friday', base=Sun 08:00) -> {got}, wanted 2026-10-02 09:00")

    # An implausibly long span is clamped onto the start's own day. parse_span
    # itself does not understand weekday words, so exercise the clamp where it
    # actually lives — in the add_event tool.
    from personal_agent.store import Store
    from personal_agent.timeutil import TZ

    import tempfile

    probe = Store(Path(tempfile.mkdtemp()) / "span.db")
    probe.add_event("Base", "2026-09-27T08:00", "2026-09-27T09:00")
    made = probe.add_event("Same day", "2026-09-27T08:00", "Sunday 12pm")
    if made["end_at"] != "2026-09-27T12:00":
        failures.append(f"add_event('Sunday 12pm' from Sun 08:00) -> ended {made['end_at']}, "
                        "wanted 2026-09-27T12:00 (a four-hour block, not seven days)")
    if made["minutes"] != 240:
        failures.append(f"clamped event duration is {made['minutes']} min, wanted 240")

    # Durations
    for text, expected in [("90m", 90), ("1.5h", 90), ("2 hours", 120), ("45", 45), ("1 week", 10080)]:
        got = parse_duration(text)
        if got != expected:
            failures.append(f"parse_duration({text!r}) -> {got}, wanted {expected}")

    # Spans
    span = parse_span("to 5pm", d(0, 14))
    if span != d(0, 17):
        failures.append(f"parse_span('to 5pm', 14:00) -> {span}, wanted 17:00")
    span = parse_span("90m", d(0, 14))
    if span != d(0, 15, 30):
        failures.append(f"parse_span('90m', 14:00) -> {span}, wanted 15:30")

    # Humanising
    if humanize(TZ.now()) != "now":
        failures.append(f"humanize(now) -> {humanize(TZ.now())!r}, wanted 'now'")
    if "in 1h" not in humanize(TZ.now() + timedelta(hours=1)):
        failures.append(f"humanize(+1h) -> {humanize(TZ.now() + timedelta(hours=1))!r}")

    # ISO round-trip
    stamp = "2026-09-26T15:30"
    if from_iso(stamp) != datetime(2026, 9, 26, 15, 30):
        failures.append(f"from_iso({stamp!r}) -> {from_iso(stamp)}")

    if failures:
        print(f"FAILED ({len(failures)})\n")
        for f in failures:
            print("  •", f)
        return 1
    print(f"passed {len(CASES) + 15} time checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
