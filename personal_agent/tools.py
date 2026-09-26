"""The tool surface. Both the local engine and any LLM call into these.

Each tool is a plain function over `Store`, registered in `REGISTRY` with a JSON
schema so an LLM can be handed the same descriptions. Keeping every mutation
here is what makes the local engine and a future OpenAI-backed engine behave
identically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .store import Store
from .timeutil import (
    TZ,
    fmt_date,
    fmt_range,
    humanize,
    parse_duration,
    parse_when,
)


class ToolError(Exception):
    """Raised for bad input so engines can report it instead of crashing."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., Any]
    group: str = "general"
    mutating: bool = False

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": {k: {**v} for k, v in properties.items()},
        "required": required or [],
    }


STR = {"type": "string"}
NUM = {"type": "integer"}
STR_LIST = {"type": "array", "items": {"type": "string"}}
BOOL = {"type": "boolean"}


def _dt_arg(value: str | None, field_name: str, required: bool = False) -> str | None:
    if not value:
        if required:
            raise ToolError(f"{field_name} is required")
        return None
    parsed = parse_when(value)
    if not parsed:
        raise ToolError(f"could not understand {field_name}: {value!r}")
    return parsed.strftime("%Y-%m-%dT%H:%M")


# --------------------------------------------------------------------------- #
# tasks
# --------------------------------------------------------------------------- #
def add_task(store: Store, title: str, due: str | None = None, priority: int = 2,
             project: str = "", notes: str = "") -> dict:
    if not title:
        raise ToolError("title is required")
    task = store.add_task(title.strip(), due_at=_dt_arg(due, "due"),
                          priority=priority, project=project, notes=notes)
    return {"task": task, "message": f"Added task: {task['title']}"}


def list_tasks(store: Store, scope: str = "open", project: str = "", limit: int = 25,
               day: str = "") -> dict:
    now = TZ.now()
    scope = (scope or "open").lower()
    if day:
        # An explicit day always wins over the scope, so "what's due tomorrow"
        # never gets answered with today's list.
        target = parse_when(day)
        if target:
            rows = store.list_tasks(status=list(("open", "doing", "blocked")), limit=500)
            rows = [t for t in rows if t.get("due_at")
                    and t["due_at"][:10] == target.strftime("%Y-%m-%d")]
            return {"tasks": rows, "count": len(rows), "day": fmt_date(target),
                    "message": f"{len(rows)} task(s) due {fmt_date(target)}" if rows
                    else f"Nothing due {fmt_date(target)}."}
    if scope in {"today", "due today"}:
        rows = store.list_tasks(status=list(("open", "doing", "blocked")), limit=500)
        rows = [t for t in rows if t.get("due_today")]
    elif scope in {"week", "this week", "next week"}:
        from datetime import timedelta
        rows = store.list_tasks(status=list(("open", "doing", "blocked")), limit=500)
        rows = [t for t in rows if t.get("due_at") and t["due_at"] <= (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")]
    elif scope in {"overdue", "late"}:
        rows = [t for t in store.list_tasks(status=list(("open", "doing", "blocked")), limit=500) if t.get("overdue")]
    elif scope in {"done", "completed"}:
        rows = store.list_tasks(status="done", limit=limit)
    elif scope == "all":
        rows = store.list_tasks(status="all", limit=limit)
    else:
        rows = store.list_tasks(status=list(("open", "doing", "blocked")), project=project or None, limit=limit)
    return {
        "tasks": rows,
        "count": len(rows),
        "message": f"{len(rows)} task(s)" if rows else "No matching tasks.",
    }


def complete_task(store: Store, id: int) -> dict:
    task = store.complete_task(int(id))
    if not task:
        raise ToolError(f"no task with id {id}")
    return {"task": task, "message": f"Completed: {task['title']}"}


def update_task(store: Store, id: int, **fields) -> dict:
    task = store.update_task(int(id), **fields)
    if not task:
        raise ToolError(f"no task with id {id}")
    return {"task": task, "message": f"Updated: {task['title']}"}


def delete_task(store: Store, id: int) -> dict:
    if not store.delete_task(int(id)):
        raise ToolError(f"no task with id {id}")
    return {"message": "Task deleted."}


# --------------------------------------------------------------------------- #
# events / schedule
# --------------------------------------------------------------------------- #
def add_event(store: Store, title: str, start: str, end: str = "", kind: str = "class",
              location: str = "", notes: str = "") -> dict:
    if not title:
        raise ToolError("title is required")
    start_dt = parse_when(start)
    if not start_dt:
        raise ToolError(f"could not understand start time: {start!r}")

    from datetime import timedelta
    if end:
        end_dt = parse_when(end, base=start_dt)
        # A weekday or relative phrase can resolve days away from the start
        # ("sunday 12pm" on a Sunday). If the span is implausibly long, fall
        # back to reading the clock time on the start's own day.
        if end_dt and (end_dt - start_dt) > timedelta(hours=12):
            clock = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", str(end).lower())
            if clock:
                hh = int(clock.group(1)) % 12 + (12 if clock.group(3) == "p" else 0)
                same_day = start_dt.replace(hour=hh, minute=int(clock.group(2) or 0))
                if same_day > start_dt:
                    end_dt = same_day
    else:
        minutes = parse_duration(notes) if notes else None
        end_dt = start_dt + timedelta(minutes=minutes or 60)
    event = store.add_event(title.strip(), start_dt.strftime("%Y-%m-%dT%H:%M"),
                            end_dt.strftime("%Y-%m-%dT%H:%M"), kind=kind, location=location, notes=notes)
    clash = [c for c in store.conflicts(day_bounds_str(start_dt)) if
             c["a"]["id"] == event["id"] or c["b"]["id"] == event["id"]]
    out = {"event": event, "message": f"Scheduled {event['label']} — {event['title']}"}
    if clash:
        other = clash[0]["a"] if clash[0]["b"]["id"] == event["id"] else clash[0]["b"]
        out["warning"] = f"Overlaps {other['title']} ({clash[0]['overlap_min']} min)"
    return out


def day_bounds_str(dt) -> tuple[str, str]:
    from .timeutil import day_bounds

    lo, hi = day_bounds(dt.date())
    return lo.strftime("%Y-%m-%dT%H:%M"), hi.strftime("%Y-%m-%dT%H:%M")


def list_events(store: Store, day: str = "today", kind: str = "") -> dict:
    target = parse_when(day) if day else TZ.now()
    target = target or TZ.now()
    events = store.agenda(target.date())
    if kind:
        events = [e for e in events if e["kind"] == kind]
    return {"events": events, "count": len(events), "day": fmt_date(target),
            "message": f"{len(events)} event(s) on {fmt_date(target)}"}


def find_conflicts(store: Store, day: str = "today") -> dict:
    target = parse_when(day) or TZ.now()
    clashes = store.conflicts(*day_bounds_str(target))
    return {"conflicts": clashes, "count": len(clashes),
            "message": f"{len(clashes)} conflict(s) on {fmt_date(target)}."
                       if clashes else f"No conflicts on {fmt_date(target)}."}


def free_slots(store: Store, day: str = "today") -> dict:
    target = parse_when(day) or TZ.now()
    slots = store.free_slots(target.date())
    when = fmt_date(target)
    return {"slots": slots, "count": len(slots),
            "message": f"Free on {when}: "
                       + (", ".join(fmt_range(s["start"], s["end"]) for s in slots)
                          or "nothing left — fully booked.")}


# --------------------------------------------------------------------------- #
# memory
# --------------------------------------------------------------------------- #
def remember(store: Store, text: str, kind: str = "fact") -> dict:
    entry = store.remember(text, kind=kind)
    return {"memory": entry, "message": f"Remembered: {text}"}


def recall(store: Store, query: str = "") -> dict:
    rows = store.recall(query or None)
    return {"memory": rows, "count": len(rows),
            "message": "\n".join(f"- {r['text']}" for r in rows) or "Nothing stored yet."}


def forget(store: Store, id: int) -> dict:
    if not store.forget(int(id)):
        raise ToolError(f"no memory with id {id}")
    return {"message": "Forgotten."}


# --------------------------------------------------------------------------- #
# drafts
# --------------------------------------------------------------------------- #
def create_draft(store: Store, audience: str, subject: str, body: str, channel: str = "email") -> dict:
    draft = store.add_draft(channel=channel, audience=audience, subject=subject, body=body)
    return {"draft": draft, "message": f"Draft saved for {audience}."}


def list_drafts(store: Store) -> dict:
    rows = store.list_drafts()
    return {"drafts": rows, "count": len(rows),
            "message": f"{len(rows)} draft(s)." if rows else "No drafts yet."}


# --------------------------------------------------------------------------- #
# synthesis
# --------------------------------------------------------------------------- #
def daily_brief(store: Store) -> dict:
    data = store.overview()
    counts = data["counts"]
    focus = data["focus_task"]
    parts = [
        f"{data['greeting']}, {store.get_setting('owner', 'Shekhar')}. "
        f"{counts['due_today']} task(s) due today, {counts['overdue']} overdue."
    ]
    if focus:
        parts.append(f"Start with: {focus['title']} ({focus['priority_label']}).")
    if data["today"]:
        parts.append("Today: " + "; ".join(f"{e['time_label']} {e['title']}" for e in data["today"][:5]) + ".")
    if data["free_slots"]:
        parts.append(f"Free: {fmt_range(data['free_slots'][0]['start'], data['free_slots'][0]['end'])}.")
    return {"overview": data, "message": " ".join(parts)}


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
REGISTRY: list[Tool] = [
    Tool("add_task", "Create a task with an optional due date.", _obj(
        {"title": STR, "due": {**STR, "description": "Natural language, e.g. 'tomorrow 5pm'"},
         "priority": {**NUM, "description": "1 = highest"}, "project": STR, "notes": STR}, ["title"]),
        add_task, "tasks", mutating=True),
    Tool("list_tasks", "List tasks by scope: open|today|week|overdue|done|all. "
                       "Pass `day` to list what is due on a specific day.", _obj(
        {"scope": STR, "day": {**STR, "description": "e.g. 'tomorrow', 'friday'"},
         "project": STR, "limit": NUM}),
        list_tasks, "tasks"),
    Tool("complete_task", "Mark a task done by id.", _obj({"id": NUM}, ["id"]), complete_task, "tasks", mutating=True),
    Tool("update_task", "Update fields on a task.", _obj(
        {"id": NUM, "title": STR, "status": STR, "priority": NUM, "project": STR, "due": STR}, ["id"]),
        update_task, "tasks", mutating=True),
    Tool("delete_task", "Delete a task by id.", _obj({"id": NUM}, ["id"]), delete_task, "tasks", mutating=True),

    Tool("add_event", "Block time on the calendar. start/end accept natural language.", _obj(
        {"title": STR, "start": STR, "end": STR, "kind": {**STR, "enum": ["class", "meeting", "study", "personal", "deadline", "travel"]},
         "location": STR, "notes": STR}, ["title", "start"]),
        add_event, "schedule", mutating=True),
    Tool("list_events", "Agenda for a given day.", _obj({"day": STR, "kind": STR}), list_events, "schedule"),
    Tool("find_conflicts", "Detect overlapping calendar events.", _obj({"day": STR}), find_conflicts, "schedule"),
    Tool("free_slots", "Unbooked time in the working day.", _obj({"day": STR}), free_slots, "schedule"),

    Tool("remember", "Store a durable fact, preference or commitment.", _obj(
        {"text": STR, "kind": {**STR, "enum": ["fact", "preference", "commitment", "project"]}}, ["text"]),
        remember, "memory", mutating=True),
    Tool("recall", "Search stored memory.", _obj({"query": STR}), recall, "memory"),
    Tool("forget", "Delete a memory by id.", _obj({"id": NUM}, ["id"]), forget, "memory", mutating=True),

    Tool("create_draft", "Save an email/message draft in Shekhar's voice.", _obj(
        {"audience": STR, "subject": STR, "body": STR, "channel": {**STR, "enum": ["email", "message", "note"]}},
        ["audience", "body"]),
        create_draft, "comms", mutating=True),
    Tool("list_drafts", "List saved drafts.", _obj({}), list_drafts, "comms"),
    Tool("daily_brief", "Unified summary of tasks, agenda, conflicts and free time.", _obj({}),
        daily_brief, "synthesis"),
]

BY_NAME = {t.name: t for t in REGISTRY}


def run_tool(store: Store, name: str, args: dict | None = None) -> dict:
    """Execute a tool by name. Raises ToolError on unknown tool / bad args."""
    tool = BY_NAME.get(name)
    if not tool:
        raise ToolError(f"unknown tool: {name}")
    args = {k: v for k, v in (args or {}).items() if v is not None}
    try:
        return tool.func(store, **args)
    except ToolError:
        raise
    except TypeError as exc:
        raise ToolError(f"bad arguments for {name}: {exc}") from exc
    except (ValueError, KeyError) as exc:
        # Store-layer validation (blank title, missing field) must surface as a
        # ToolError so every engine reports it the same way.
        raise ToolError(f"{name}: {exc}") from exc
