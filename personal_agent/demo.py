"""Optional sample data so a fresh install has something to look at."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from .store import Store
from .timeutil import TZ


def _at(day_offset: int, hour: int, minute: int = 0) -> str:
    target = TZ.today() + timedelta(days=day_offset)
    return target.strftime("%Y-%m-%d") + f"T{hour:02d}:{minute:02d}"


def _span(day_offset: int, hour: int, minutes: int) -> tuple[str, str]:
    """(start, end) for a block that may roll past midnight safely."""
    start = datetime.combine(TZ.today() + timedelta(days=day_offset), time(hour, 0))
    return _iso(start), _iso(start + timedelta(minutes=minutes))


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def seed(store: Store) -> dict:
    if store.task_counts()["total"] or store.list_events(limit=1):
        return {"status": "skipped", "reason": "database already has data"}

    events = [
        ("Linear Algebra lecture", 0, 9, 60, "class", "Room B-204"),
        ("Physics lab", 0, 14, 120, "class", "Lab 3"),
        ("Study block — Thermodynamics", 0, 17, 120, "study", "Library"),
        ("Algorithms tutorial", 1, 11, 60, "class", "Room A-110"),
        ("Project sync with team", 1, 16, 45, "meeting", "Zoom"),
        ("DSA problem set", 2, 10, 90, "class", "Room C-301"),
    ]
    for title, off, h, dur, kind, loc in events:
        start, end = _span(off, h, dur)
        store.add_event(title, start, end, kind=kind, location=loc)

    tasks = [
        ("Submit thermodynamics problem set", 0, 23, 1, "Physics"),
        ("Read chapter 6 — Fourier series", 1, 20, 3, "Physics"),
        ("Draft project proposal outline", 2, 18, 1, "Project"),
        ("Email supervisor about TA hours", 1, 12, 2, "Admin"),
        ("Revise linear algebra for midterm", 4, 21, 1, "Study"),
        ("Book dentist appointment", 6, 17, 3, "Personal"),
    ]
    for title, off, h, p, proj in tasks:
        store.add_task(title, due_at=_at(off, h), priority=p, project=proj)

    store.add_task("Renew library books", due_at=_at(-2, 18), priority=2, project="Admin")  # overdue on purpose
    store.remember("Prefers deep-work and study blocks before noon.", kind="preference")
    store.remember("Physics midterm is in about two weeks.", kind="commitment")
    store.add_draft(
        channel="email", audience="Supervisor",
        subject="TA hours for the coming term",
        body=("Hi,\n\nHope you're well. I wanted to check in about TA hours for the coming term — "
              "I'm available Tuesday and Thursday afternoons, and could also cover a Friday lab slot "
              "if that helps.\n\nLet me know what works best.\n\nThanks,\nShekhar"),
    )
    return {"status": "seeded", "events": len(events), "tasks": len(tasks) + 1}
