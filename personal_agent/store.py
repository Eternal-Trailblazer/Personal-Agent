"""SQLite storage layer — the single source of truth for Personal Agent.

Plain stdlib sqlite3, WAL mode, one connection per thread. Every mutation goes
through here so the dashboard, the CLI and the brain all share identical
semantics.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from . import config
from .timeutil import TZ, day_bounds, fmt_date, fmt_range, humanize, overlaps

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT    NOT NULL,
    notes        TEXT    DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'open',
    priority     INTEGER NOT NULL DEFAULT 2,
    project      TEXT    DEFAULT '',
    due_at       TEXT,
    created_at   TEXT    NOT NULL,
    completed_at TEXT,
    sort_order   REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    kind       TEXT    NOT NULL DEFAULT 'class',
    location   TEXT    DEFAULT '',
    notes      TEXT    DEFAULT '',
    start_at   TEXT    NOT NULL,
    end_at     TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS memory (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL DEFAULT 'fact',
    text       TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '',
    pinned     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT NOT NULL DEFAULT 'email',
    audience   TEXT NOT NULL DEFAULT '',
    subject    TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status   ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_due      ON tasks(due_at);
CREATE INDEX IF NOT EXISTS idx_events_start   ON events(start_at);
"""

TASK_STATUSES = ("open", "doing", "blocked", "done", "dropped")
OPEN_STATUSES = ("open", "doing", "blocked")
EVENT_KINDS = ("class", "meeting", "study", "personal", "deadline", "travel")
PRIORITY_LABELS = {1: "P1", 2: "P2", 3: "P3"}


def _now() -> str:
    return TZ.now().replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


class Store:
    """Thread-safe SQLite wrapper."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else config.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # -- plumbing -------------------------------------------------------- #
    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _q(self, sql: str, params: Iterable = ()) -> list[dict]:
        cur = self.connect().execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]

    def _w(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        return self.connect().execute(sql, tuple(params))

    def _one(self, sql: str, params: Iterable = ()) -> dict | None:
        rows = self._q(sql, params)
        return rows[0] if rows else None

    # -- settings -------------------------------------------------------- #
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self._w(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    # -- tasks ----------------------------------------------------------- #
    def add_task(
        self,
        title: str,
        *,
        notes: str = "",
        due_at: str | None = None,
        priority: int = 2,
        project: str = "",
        status: str = "open",
    ) -> dict:
        title = (title or "").strip()
        if not title:
            raise ValueError("task title is required")
        status = status if status in TASK_STATUSES else "open"
        priority = max(1, min(3, int(priority or 2)))
        due_at = _iso_or_die(due_at, "due_at") if due_at else None
        nxt = self._one("SELECT COALESCE(MAX(sort_order), 0) AS m FROM tasks")["m"] + 1
        cur = self._w(
            "INSERT INTO tasks(title, notes, status, priority, project, due_at, created_at, sort_order)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (title, notes or "", status, priority, project or "", due_at, _now(), nxt),
        )
        return self.get_task(cur.lastrowid)

    def get_task(self, task_id: int) -> dict | None:
        row = self._one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return self._shape_task(row) if row else None

    def update_task(self, task_id: int, **fields) -> dict | None:
        allowed = {"title", "notes", "status", "priority", "project", "due_at", "sort_order"}
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "status":
                value = value if value in TASK_STATUSES else "open"
            if key == "priority":
                value = max(1, min(3, int(value or 2)))
            if key == "due_at":
                value = _iso_or_die(value, "due_at") if value else None
            sets.append(f"{key}=?")
            params.append(value)
        if fields.get("status") == "done" and not fields.get("completed_at"):
            sets.append("completed_at=?")
            params.append(_now())
        if fields.get("status") and fields["status"] != "done":
            sets.append("completed_at=NULL")
        if not sets:
            return self.get_task(task_id)
        params.append(task_id)
        self._w(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", params)
        return self.get_task(task_id)

    def complete_task(self, task_id: int) -> dict | None:
        return self.update_task(task_id, status="done")

    def delete_task(self, task_id: int) -> bool:
        return self._w("DELETE FROM tasks WHERE id=?", (task_id,)).rowcount > 0

    def list_tasks(
        self,
        *,
        status: str | list[str] | None = None,
        project: str | None = None,
        due_before: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if status == "all":
            pass
        elif status:
            wanted = [status] if isinstance(status, str) else list(status)
            wanted = [s for s in wanted if s in TASK_STATUSES]
            if wanted:
                sql += f" AND status IN ({','.join('?' * len(wanted))})"
                params += wanted
        else:
            sql += " AND status != 'dropped'"
        if project:
            sql += " AND project=?"
            params.append(project)
        if due_before:
            sql += " AND due_at IS NOT NULL AND due_at <= ?"
            params.append(due_before)
        sql += " ORDER BY status='done', priority, (due_at IS NULL), due_at, sort_order LIMIT ?"
        params.append(int(limit))
        return [self._shape_task(t) for t in self._q(sql, params)]

    def _shape_task(self, row: dict) -> dict:
        now = TZ.now()
        due = _dt(row.get("due_at"))
        row["due_human"] = humanize(due, now) if due else ""
        row["due_label"] = fmt_date(due) if due else ""
        row["priority_label"] = PRIORITY_LABELS.get(row.get("priority", 2), "P2")
        row["overdue"] = bool(due and due < now and row.get("status") in OPEN_STATUSES)
        row["due_today"] = bool(due and due.date() == now.date())
        return row

    def task_counts(self) -> dict:
        now = TZ.now()
        rows = self._q("SELECT status, COUNT(*) c FROM tasks GROUP BY status")
        counts = {r["status"]: r["c"] for r in rows}
        open_ct = sum(counts.get(s, 0) for s in OPEN_STATUSES)
        overdue = self._one(
            "SELECT COUNT(*) c FROM tasks WHERE status IN (?,?,?) AND due_at IS NOT NULL AND due_at < ?",
            (*OPEN_STATUSES, now.strftime("%Y-%m-%dT%H:%M")),
        )["c"]
        today = self._one(
            "SELECT COUNT(*) c FROM tasks WHERE status IN (?,?,?) AND due_at >= ? AND due_at <= ?",
            (*OPEN_STATUSES, *day_bounds(now.date())),
        )["c"]
        week = self._one(
            "SELECT COUNT(*) c FROM tasks WHERE status IN (?,?,?) AND due_at >= ? AND due_at <= ?",
            (*OPEN_STATUSES, *day_bounds(now.date() + timedelta(days=7))),
        )["c"]
        return {
            "open": open_ct,
            "done": counts.get("done", 0),
            "doing": counts.get("doing", 0),
            "blocked": counts.get("blocked", 0),
            "overdue": overdue,
            "due_today": today,
            "due_week": week,
            "total": sum(counts.values()),
        }

    def projects(self) -> list[str]:
        rows = self._q(
            "SELECT project, COUNT(*) c FROM tasks WHERE project != '' GROUP BY project ORDER BY c DESC"
        )
        return [r["project"] for r in rows]

    # -- events ---------------------------------------------------------- #
    def add_event(
        self,
        title: str,
        start_at: str,
        end_at: str,
        *,
        kind: str = "class",
        location: str = "",
        notes: str = "",
    ) -> dict:
        title = (title or "").strip()
        if not title:
            raise ValueError("event title is required")
        kind = kind if kind in EVENT_KINDS else "class"
        # Normalise to ISO wall-clock so sorting, conflict detection and labels
        # all work regardless of whether the caller passed "today 15:00" or
        # "2026-09-26T15:00".
        start_iso = _iso_or_die(start_at, "start_at")
        end_iso = _iso_or_die(end_at, "end_at", fallback=start_iso)
        if end_iso <= start_iso:
            raise ValueError("end_at must be after start_at")
        cur = self._w(
            "INSERT INTO events(title, kind, location, notes, start_at, end_at, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (title, kind, location or "", notes or "", start_iso, end_iso, _now()),
        )
        return self.get_event(cur.lastrowid)

    def get_event(self, event_id: int) -> dict | None:
        row = self._one("SELECT * FROM events WHERE id=?", (event_id,))
        return self._shape_event(row) if row else None

    def update_event(self, event_id: int, **fields) -> dict | None:
        allowed = {"title", "kind", "location", "notes", "start_at", "end_at"}
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                if key == "kind":
                    value = value if value in EVENT_KINDS else "class"
                if key in {"start_at", "end_at"} and value:
                    value = _iso_or_die(value, key)
                sets.append(f"{key}=?")
                params.append(value)
        if not sets:
            return self.get_event(event_id)
        params.append(event_id)
        self._w(f"UPDATE events SET {', '.join(sets)} WHERE id=?", params)
        return self.get_event(event_id)

    def delete_event(self, event_id: int) -> bool:
        return self._w("DELETE FROM events WHERE id=?", (event_id,)).rowcount > 0

    def list_events(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        sql = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []
        if start:
            sql += " AND end_at >= ?"
            params.append(start)
        if end:
            sql += " AND start_at <= ?"
            params.append(end)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY start_at LIMIT ?"
        params.append(int(limit))
        return [self._shape_event(e) for e in self._q(sql, params)]

    def _shape_event(self, row: dict) -> dict:
        start, end = _dt(row.get("start_at")), _dt(row.get("end_at"))
        row["start_human"] = humanize(start) if start else ""
        row["label"] = fmt_range(start, end)
        row["day_label"] = fmt_date(start)
        row["time_label"] = fmt_range(start, end)
        row["minutes"] = int((end - start).total_seconds() // 60) if start and end else 0
        return row

    def conflicts(self, start: str | None = None, end: str | None = None) -> list[dict]:
        """Overlapping events in the window, as pairs grouped per day."""
        if not start or not end:
            lo, hi = day_bounds(TZ.today())
            start, end = lo.strftime("%Y-%m-%dT%H:%M"), hi.strftime("%Y-%m-%dT%H:%M")
        events = [self._shape_event(e) for e in self.list_events(start=start, end=end, limit=500)]
        events.sort(key=lambda e: e["start_at"])
        clashes = []
        for i in range(len(events) - 1):
            a, b = events[i], events[i + 1]
            a_start, a_end = _dt(a["start_at"]), _dt(a["end_at"])
            b_start, b_end = _dt(b["start_at"]), _dt(b["end_at"])
            if overlaps(a_start, a_end, b_start, b_end):
                clashes.append({
                    "a": a,
                    "b": b,
                    "overlap_min": int((min(a_end, b_end) - max(a_start, b_start)).total_seconds() // 60),
                    "day": fmt_date(a_start),
                })
        return clashes

    def agenda(self, day=None) -> list[dict]:
        day = day or TZ.today()
        lo, hi = day_bounds(day)
        return self.list_events(
            start=lo.strftime("%Y-%m-%dT%H:%M"),
            end=hi.strftime("%Y-%m-%dT%H:%M"),
        )

    def free_slots(self, day=None, work_start: int = 9, work_end: int = 21) -> list[dict]:
        """Unbooked windows inside the working day, 30-min granularity."""
        from .timeutil import from_iso

        day = day or TZ.today()
        lo, hi = day_bounds(day)
        cursor = lo.replace(hour=work_start, minute=0)
        close = lo.replace(hour=work_end, minute=0)
        if cursor >= close:
            return []
        booked = [(from_iso(e["start_at"]), from_iso(e["end_at"])) for e in self.agenda(day)]
        slots, current = [], cursor
        for b_start, b_end in sorted(booked):
            if b_end <= current or b_start >= close:
                continue
            if b_start > current:
                slots.append({"start": current.strftime("%Y-%m-%dT%H:%M"), "end": b_start.strftime("%Y-%m-%dT%H:%M"),
                              "minutes": int((b_start - current).total_seconds() // 60)})
            current = max(current, b_end)
        if current < close:
            slots.append({"start": current.strftime("%Y-%m-%dT%H:%M"), "end": close.strftime("%Y-%m-%dT%H:%M"),
                          "minutes": int((close - current).total_seconds() // 60)})
        return [s for s in slots if s["minutes"] >= 30]

    # -- memory ---------------------------------------------------------- #
    def remember(self, text: str, *, kind: str = "fact", tags: str = "", pinned: bool = False) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("memory text is required")
        cur = self._w(
            "INSERT INTO memory(kind, text, tags, pinned, created_at) VALUES(?,?,?,?,?)",
            (kind, text, tags or "", 1 if pinned else 0, _now()),
        )
        return self.get_memory(cur.lastrowid)

    def get_memory(self, mem_id: int) -> dict | None:
        return self._one("SELECT * FROM memory WHERE id=?", (mem_id,))

    def recall(self, query: str | None = None, *, limit: int = 100) -> list[dict]:
        if query:
            like = f"%{query}%"
            return self._q(
                "SELECT * FROM memory WHERE text LIKE ? OR tags LIKE ?"
                " ORDER BY pinned DESC, created_at DESC LIMIT ?",
                (like, like, int(limit)),
            )
        return self._q("SELECT * FROM memory ORDER BY pinned DESC, created_at DESC LIMIT ?", (int(limit),))

    def forget(self, mem_id: int) -> bool:
        return self._w("DELETE FROM memory WHERE id=?", (mem_id,)).rowcount > 0

    def memory_log(self) -> str:
        """Working-memory block injected into the brain's system prompt."""
        rows = self.recall(limit=40)
        if not rows:
            return ""
        lines = [f"- [{r['kind']}] {r['text']}" + (" (pinned)" if r["pinned"] else "") for r in rows]
        return "\n".join(lines)

    # -- drafts ---------------------------------------------------------- #
    def add_draft(self, *, channel: str, audience: str, subject: str, body: str, status: str = "draft") -> dict:
        cur = self._w(
            "INSERT INTO drafts(channel, audience, subject, body, status, created_at) VALUES(?,?,?,?,?,?)",
            (channel or "email", audience or "", subject or "", body or "", status, _now()),
        )
        return self.get_draft(cur.lastrowid)

    def get_draft(self, draft_id: int) -> dict | None:
        return self._one("SELECT * FROM drafts WHERE id=?", (draft_id,))

    def list_drafts(self, *, status: str | None = "draft", limit: int = 50) -> list[dict]:
        if status == "all":
            return self._q("SELECT * FROM drafts ORDER BY created_at DESC LIMIT ?", (limit,))
        return self._q(
            "SELECT * FROM drafts WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)
        )

    def update_draft(self, draft_id: int, **fields) -> dict | None:
        allowed = {"channel", "audience", "subject", "body", "status"}
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key}=?")
                params.append(value)
        if not sets:
            return self.get_draft(draft_id)
        params.append(draft_id)
        self._w(f"UPDATE drafts SET {', '.join(sets)} WHERE id=?", params)
        return self.get_draft(draft_id)

    def delete_draft(self, draft_id: int) -> bool:
        return self._w("DELETE FROM drafts WHERE id=?", (draft_id,)).rowcount > 0

    # -- dashboard ------------------------------------------------------- #
    def overview(self) -> dict:
        now = TZ.now()
        today = self.agenda()
        upcoming = self.list_events(start=now.strftime("%Y-%m-%dT%H:%M"), limit=8)
        free = self.free_slots()
        return {
            "generated_at": _now(),
            "greeting": self._greeting(now),
            "tz": TZ.name,
            "tz_label": TZ.offset_label(),
            "counts": self.task_counts(),
            "today": today,
            "upcoming": upcoming,
            "conflicts": self.conflicts(),
            "free_slots": free,
            "focus_task": self._focus_task(),
            "drafts": len(self.list_drafts()),
            "memory": len(self.recall(limit=500)),
        }

    @staticmethod
    def _greeting(now: datetime) -> str:
        hour = now.hour
        if hour < 12:
            return "Good morning"
        if hour < 17:
            return "Good afternoon"
        return "Good evening"

    def _focus_task(self) -> dict | None:
        """Single highest-leverage open task: P1 first, then nearest deadline."""
        rows = self.list_tasks(status=list(OPEN_STATUSES), limit=50)
        if not rows:
            return None
        return sorted(
            rows,
            key=lambda t: (
                t["priority"],
                0 if t.get("due_at") else 1,
                t.get("due_at") or "9999",
            ),
        )[0]

    def global_search(self, query: str, limit: int = 12) -> dict:
        like = f"%{query}%"
        return {
            "tasks": self._q("SELECT * FROM tasks WHERE title LIKE ? OR notes LIKE ? LIMIT ?", (like, like, limit)),
            "events": self._q("SELECT * FROM events WHERE title LIKE ? OR location LIKE ? LIMIT ?", (like, like, limit)),
            "memory": self._q("SELECT * FROM memory WHERE text LIKE ? LIMIT ?", (like, limit)),
            "drafts": self._q("SELECT * FROM drafts WHERE subject LIKE ? OR body LIKE ? LIMIT ?", (like, like, limit)),
        }


def _dt(value: str | None):
    from .timeutil import from_iso

    return from_iso(value)


def _iso_or_die(value, field_name: str, fallback: str | None = None) -> str:
    """Coerce a timestamp to ISO wall-clock, accepting natural language.

    Raises ValueError when the input can't be understood, so a malformed
    event can never be persisted and silently break conflict detection.
    """
    from .timeutil import from_iso, parse_when

    if not value:
        if fallback:
            return fallback
        raise ValueError(f"{field_name} is required")
    if isinstance(value, datetime):
        return to_iso(value)
    parsed = from_iso(value)
    if not parsed:
        parsed = parse_when(value)
    if not parsed:
        raise ValueError(f"could not understand {field_name}: {value!r}")
    return to_iso(parsed)


def to_iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M")
