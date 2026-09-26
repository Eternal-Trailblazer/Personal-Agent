"""Engine interface — the seam that keeps Personal Agent hybrid.

Two implementations ship in the box:

* ``LocalEngine``  — deterministic intent parser, zero network, zero cost.
                     Always available; this is the default.
* ``OpenAIEngine`` — OpenAI-compatible chat-completions with tool calling.
                     Opt-in via settings or ``PA_LLM_API_KEY``.

``OpenAIEngine`` degrades to ``LocalEngine`` on any network/auth error, so the
app never becomes unusable because a provider is down. Swap in Anthropic, Gemini
or a local Ollama endpoint by implementing ``Engine`` — nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..config import LLMSettings, load_llm_settings
from ..prompt import build_system_prompt
from ..store import Store
from ..tools import ToolError, run_tool


@dataclass
class Reply:
    """What every engine returns, regardless of who did the thinking."""

    text: str
    actions: list[dict] = field(default_factory=list)
    engine: str = "local"
    degraded: bool = False
    note: str = ""
    # True when the local engine recognised the request but deliberately did not
    # act on it (its "I didn't catch a specific action" path). The hybrid router
    # uses this to decide whether to escalate to the cloud.
    deferred: bool = False


@runtime_checkable
class Engine(Protocol):
    name: str

    def available(self) -> bool: ...

    def respond(self, message: str, history: list[dict] | None = None) -> Reply: ...


# --------------------------------------------------------------------------- #
# local engine
# --------------------------------------------------------------------------- #
import re  # noqa: E402  (kept local to the engine section)

from ..timeutil import TZ, fmt_range, humanize, parse_when  # noqa: E402


class LocalEngine:
    """Rule-based intent parser.

    Deliberately narrow: it handles the high-frequency phrasings and hands
    anything ambiguous to a clarifying question rather than guessing. This is the
    safety net the whole hybrid design rests on.
    """

    name = "local"

    def __init__(self, store: Store):
        self.store = store
        self._deferred = False

    def available(self) -> bool:
        return True

    def respond(self, message: str, history: list[dict] | None = None) -> Reply:
        text = (message or "").strip()
        if not text:
            return Reply("Say something and I'll act on it.", engine=self.name)

        low = text.lower().strip()
        actions: list[dict] = []
        self._deferred = False

        try:
            handler = self._match(low, text)
            if handler is None:
                return Reply(self._fallback(text), engine=self.name, deferred=True)
            reply = handler(low, text, actions)
        except ToolError as exc:
            return Reply(f"Couldn't do that: {exc}", engine=self.name)

        if reply is None:
            return Reply(self._fallback(text), engine=self.name, deferred=True)
        if self._deferred:
            # The local engine recognised the shape of the request but refuses
            # to guess. Hand it to the cloud rather than inventing an answer.
            return Reply(reply, engine=self.name, deferred=True)
        return Reply(reply, actions=actions, engine=self.name)

    # -- routing --------------------------------------------------------- #
    def _match(self, low: str, raw: str):
        # Order matters — first match wins. Review queries are checked before
        # capture verbs so "what do you remember" never lands on _remember.
        # A leading "please"/"can you" is tolerated on every command form.
        p = r"^\s*(?:please\s+|can you\s+|could you\s+)?"
        table = [
            (r"^\s*\d+\s*[\.\)]\s+", self._plan),
            (r"\b(what do you (remember|know)|recall\b|what'?s in (your )?memory)", self._recall),
            # "what's due ..." is a task question, never a daily brief — the
            # brief rule below would otherwise swallow it and answer about the
            # whole day instead of the deadline.
            (r"\b(what|whats|what's|show|list|which)\b.{0,20}\b(due|left|pending|coming up)\b", self._today),
            (r"\b(what'?s|what is|give me|show|summari[sz]e|recap|status of|update)\b.*\b(today|now|agenda|brief|schedule|plan)\b", self._brief),
            (r"\bbrief(ing)?\b", self._brief),
            (r"\b(what|whats|show|list).{0,14}\b(today|left|pending|due|next|open)\b", self._today),
            (r"\b(overdue|late|behind|slipping)\b", self._overdue),
            (r"\b(conflict|clash|overlap|double.?book)", self._conflicts),
            (r"\b(free|open|available|gap|slot)s?\b", self._free),
            # Capture forms must be tested before the bare "task(s)" noun, or
            # "add a task to email X" gets read as a request to list tasks.
            (p + r"(?:add|create|new|put in|track|capture|log|remind me to|remind me|i need to)\b", self._add_task),
            (p + r"(?:task|todo|to-do)\b\s*[:\-]?\s+\S", self._add_task),
            (p + r"(?:schedule|book|block|slot in|set up|plan)\b", self._add_event),
            (r"\b(schedule|book|slot in)\b.*\b(class|meeting|lecture|tutorial|tut|session|seminar)\b", self._add_event),
            (r"\b(complete|done|finished|finish|mark .{0,10}done|check off)\b", self._complete),
            (r"\b(remember|keep in mind|don'?t forget|note down)\b", self._remember),
            (p + r"(?:draft|write|compose|reply to|respond to|email)\b", self._draft),
            (r"\b(tasks?|todo|to-do)\b", self._list_tasks),
        ]
        for pattern, fn in table:
            if re.search(pattern, low):
                return fn
        if "\n" in raw.strip():
            return self._plan
        return None

    # -- intents --------------------------------------------------------- #
    def _brief(self, low, raw, actions) -> str:
        res = run_tool(self.store, "daily_brief")
        actions.append({"tool": "daily_brief"})
        return res["message"]

    def _today(self, low, raw, actions) -> str:
        # Honour an explicitly named day: "what's due tomorrow" must not answer
        # with today's list.
        day = _extract_day(raw)
        scope = "today" if not day or day == "today" else "open"
        res = run_tool(self.store, "list_tasks", {"scope": scope, "day": day or ""})
        actions.append({"tool": "list_tasks", "args": {"scope": scope, "day": day}})
        if not res["tasks"]:
            when = res.get("day") or "today"
            return f"Nothing due {when}."
        label = res.get("day") or "today"
        lines = [f"- [{t['priority_label']}] {t['title']}" for t in res["tasks"]]
        return f"{len(res['tasks'])} due {label}:\n" + "\n".join(lines)

    def _overdue(self, low, raw, actions) -> str:
        res = run_tool(self.store, "list_tasks", {"scope": "overdue"})
        actions.append({"tool": "list_tasks", "args": {"scope": "overdue"}})
        if not res["tasks"]:
            return "Nothing overdue. Clear."
        lines = [f"- {t['title']} — was due {t['due_human']}" for t in res["tasks"]]
        return f"{len(res['tasks'])} overdue:\n" + "\n".join(lines)

    def _conflicts(self, low, raw, actions) -> str:
        day = _extract_day(raw) or "today"
        res = run_tool(self.store, "find_conflicts", {"day": day})
        actions.append({"tool": "find_conflicts", "args": {"day": day}})
        if not res["conflicts"]:
            return res["message"]
        lines = [
            f"- {c['day']}: {c['a']['title']} vs {c['b']['title']} ({c['overlap_min']} min overlap)"
            for c in res["conflicts"]
        ]
        return f"{len(res['conflicts'])} conflict(s):\n" + "\n".join(lines)

    def _free(self, low, raw, actions) -> str:
        day = _extract_day(raw) or "today"
        res = run_tool(self.store, "free_slots", {"day": day})
        actions.append({"tool": "free_slots", "args": {"day": day}})
        return res["message"]

    def _remember(self, low, raw, actions) -> str:
        content = re.sub(r"^\s*(please\s+)?(remember|note|keep in mind|don'?t forget)\s*[:\-]?\s*", "", raw, flags=re.I)
        if not content.strip():
            return "What should I remember?"
        res = run_tool(self.store, "remember", {"text": content.strip().rstrip("."), "kind": "fact"})
        actions.append({"tool": "remember", "args": {"text": content}})
        return res["message"] + " — I'll apply it from here on."

    def _recall(self, low, raw, actions) -> str:
        res = run_tool(self.store, "recall", {})
        actions.append({"tool": "recall"})
        return res["message"]

    def _draft(self, low, raw, actions) -> str:
        m = re.search(r"(?:to|for)\s+([A-Za-z][\w .'-]{0,40})", raw)
        audience = (m.group(1).strip() if m else "the recipient").strip(" .,")
        subject = ""
        m = re.search(r"subject\s*[:\-]\s*(.+)$", raw, flags=re.I | re.M)
        if m:
            subject = m.group(1).strip()
        body = re.sub(r"^\s*(draft|write|compose)\b.{0,40}?\s*", "", raw, flags=re.I).strip()
        if subject and body.lower().startswith(subject.lower()):
            body = body[len(subject):].lstrip(" :-")
        res = run_tool(self.store, "create_draft", {
            "audience": audience, "subject": subject or f"Re: {audience}",
            "body": body or raw, "channel": "email" if "email" in low else "message",
        })
        actions.append({"tool": "create_draft", "args": {"audience": audience}})
        return f"{res['message']}\n\n{res['draft']['body']}"

    def _add_event(self, low, raw, actions) -> str:
        # More than one day reference means a recurring or multi-day booking.
        # Correctly resolving that needs the date map, so decline to guess.
        days = {m.group(0).lower() for m in _DAY_REF.finditer(raw)}
        if len(days) > 1:
            self._deferred = True
            return ("That request covers several dates (" + ", ".join(sorted(days)) +
                    "). Connect a model in Settings and I'll book each day correctly.")

        when = _extract_when(raw)
        body = raw
        for _ in range(3):
            stripped = re.sub(
                r"^\s*(?:please\s+)?(?:schedule|book|block|slot in|put in|put|add|set up|plan)\s+"
                r"(?:a|an|the)?\s*(?:new\s+)?(?:time\s+(?:for|to)\s+)?",
                "", body, flags=re.I,
            )
            if stripped == body:
                break
            body = stripped

        keywords = ("class", "lecture", "tutorial", "tut", "seminar", "lab",
                    "meeting", "call", "sync", "study", "session", "review", "deadline")
        # The keyword doubles as the kind *and* as the title when it's the only
        # thing said — "schedule lecture 4pm" is a real event named "Lecture".
        said = next((k for k in keywords if re.search(rf"\b{k}\b", body, re.I)), None)
        title = _titlecase(_clean_title(body, drop=keywords))
        if not title:
            title = _titlecase(clean_word(said)) if said else "Busy"
        kind = _kind_from(said)

        start = when[1].strftime("%Y-%m-%dT%H:%M") if when else TZ.now().strftime("%Y-%m-%dT%H:%M")
        res = run_tool(self.store, "add_event", {"title": title, "start": start, "kind": kind})
        actions.append({"tool": "add_event", "args": {"title": title, "start": start},
                        "result": _outcome(res)})
        out = res["message"]
        if res.get("warning"):
            out += f"\nWarning: {res['warning']}"
        return out

    def _add_task(self, low, raw, actions) -> str:
        body = raw
        # Strip the capture verb and any following filler noun, repeatedly, so
        # "add task submit report" and "remind me to call mum" both reduce
        # cleanly to the bare title.
        for _ in range(3):
            stripped = re.sub(
                r"^\s*(?:please\s+)?(?:add|create|new|put|track|capture|log|"
                r"remind me to|remind me|i need to|i have to|i should|set)\s+(?:a|an|the)?\s*"
                r"(?:new\s+)?(?:task|to-do|todo|item|reminder)?\s*[:\-–]?\s*",
                "", body, flags=re.I,
            )
            if stripped == body:
                stripped = re.sub(r"^\s*(?:task|to-do|todo)\s*[:\-–]?\s+", "", body, flags=re.I)
            if stripped == body:
                break
            body = stripped
        title = _titlecase(_clean_title(body))
        if not title:
            return "What should I add?"
        due = _extract_when(raw)
        res = run_tool(self.store, "add_task", {
            "title": title,
            "due": due[1].strftime("%Y-%m-%dT%H:%M") if due else None,
        })
        actions.append({"tool": "add_task", "args": {"title": title}, "result": _outcome(res)})
        out = res["message"]
        if due:
            out += f" — due {humanize(due[1])}"
        return out

    def _complete(self, low, raw, actions) -> str:
        m = re.search(r"\b(\d+)\b", raw)
        if m:
            res = run_tool(self.store, "complete_task", {"id": int(m.group(1))})
            actions.append({"tool": "complete_task", "args": {"id": int(m.group(1))}})
            return res["message"]
        return "Which task? Give me its id, or the exact title."

    def _list_tasks(self, low, raw, actions) -> str:
        scope = "open"
        if "all" in low:
            scope = "all"
        elif "done" in low or "completed" in low:
            scope = "done"
        elif "overdue" in low:
            scope = "overdue"
        res = run_tool(self.store, "list_tasks", {"scope": scope, "limit": 20})
        actions.append({"tool": "list_tasks", "args": {"scope": scope}})
        if not res["tasks"]:
            return "Task list is clear."
        lines = [
            f"- {t['id']}. [{t['priority_label']}] {t['title']}"
            + (f" — {t['due_human']}" if t.get("due_at") else "")
            + (f"  ⚠ overdue" if t.get("overdue") else "")
            for t in res["tasks"]
        ]
        return f"{res['count']} open task(s):\n" + "\n".join(lines)

    def _plan(self, low, raw, actions) -> str:
        steps = [re.sub(r"^\s*(?:\d+[\.\)]|[-*])\s*", "", ln).strip() for ln in raw.strip().splitlines()]
        steps = [s for s in steps if len(s) > 2]
        if len(steps) < 2:
            return None or self._add_task(low, raw, actions)
        for step in steps:
            run_tool(self.store, "add_task", {"title": step})
        actions.append({"tool": "add_task", "args": {"count": len(steps)}})
        return f"Added {len(steps)} steps as tasks:\n" + "\n".join(f"- {s}" for s in steps)

    # -- fallback -------------------------------------------------------- #
    def _fallback(self, text: str) -> str:
        return (
            "I didn't catch a specific action in that. Running locally, I handle:\n"
            "- Capture: \"add task …\", \"remember …\"\n"
            "- Schedule: \"schedule lecture 4pm tomorrow\", \"conflicts tomorrow\", \"free slots today\"\n"
            "- Review: \"what's due today\", \"overdue\", \"brief\"\n"
            "- Drafts: \"draft to Prof Sharma about the deadline\"\n\n"
            "Connect a model key in Settings to unlock free-form requests."
        )


# --------------------------------------------------------------------------- #
# text extraction helpers
# --------------------------------------------------------------------------- #
_DAY_WORDS = (
    "today", "tomorrow", "tmrw", "yesterday", "tonight", "this weekend",
    "next week", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "mon", "tue", "wed", "thu", "fri", "sat", "sun",
)


def _extract_day(text: str) -> str | None:
    """First day-ish phrase in the text, else None."""
    low = (text or "").lower()
    for word in _DAY_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", low):
            return word
    match = re.search(r"\bin\s+(\d+)\s*(day|days|week|weeks)\b", low)
    if match:
        return f"in {match.group(1)} {match.group(2)}"
    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", low)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", low)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return None


_DAY_WORD = (r"(?:today|tonight|tomorrow|tmrw|tmr|yesterday|this weekend|next week|"
             r"mon|tues|wednes|thurs|fri|satur|sun)(?:day|tuesday|nesday|rsday|urday)?")
_CLOCK = r"\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?|\d{1,2}:\d{2}|\d{1,2}\s*[ap]\.?m\.?"

_WHEN_PATTERNS = (
    # clock BEFORE the day word: "4pm tomorrow", "9:30 friday"
    rf"\b(?:{_CLOCK})\s+(?:on\s+|at\s+)?(?:{_DAY_WORD})\b",
    r"\bin\s+\d+\s*(?:min|mins|minutes?|h|hrs?|hours?|d|days?|w|weeks?)\b",
    # "tomorrow", optionally with a trailing clock time
    r"\b(?:today|tomorrow|tmrw|yesterday|tonight|this weekend|next week)\b"
    r"(?:\s+(?:at\s+|around\s+)?\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?)?",
    # weekday name, optionally with a trailing clock time
    r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)(?:day|tuesday|nesday|rsday|urday)?\b"
    r"(?:\s+(?:at\s+|around\s+)?\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?)?",
    r"\b\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?)?",
    r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"(?:\s+\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?)?",
    r"\b\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?\b",
    r"\b\d{1,2}\s*(?:[ap]\.?m\.?)\b",
    r"\b(?:noon|midday|midnight|eon|end of day|eod)\b",
)


def _extract_when(text: str) -> tuple[str, Any] | None:
    """(matched_text, datetime) for the first time-ish phrase, else None.

    Patterns are ordered specific → general and each must fully parse, so
    "task" never gets its "k" mistaken for a clock time.
    """
    text = text or ""
    for pattern in _WHEN_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.I):
            phrase = match.group(0).strip()
            when = parse_when(phrase)
            if when:
                return phrase, when
    return None


# Tokens that belong to a date/time rather than to a title.
_STRIP_NOISE = re.compile(
    r"\b(?:at|on|by|before|after|due|until|till|from|to|for|around|starting)\b"
    r"|\b(?:today|tomorrow|tmrw|yesterday|tonight|noon|midday|midnight|end of day|eod)\b"
    r"|\bnext\s+(?:week|month)\b|\bthis\s+(?:week|weekend|morning|afternoon|evening)\b"
    r"|\b(?:morning|afternoon|evening)\b"
    r"|\b(?:mon|tues|wednes|thurs|fri|satur|sun)(?:day|tuesday|nesday|rsday|urday)?\b"
    r"|\b\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?\b"
    r"|\b\d{1,2}:\d{2}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?\b"
    r"|\bin\s+\d+\s*(?:min|mins|minutes?|h|hrs?|hours?|d|days?|w|weeks?)\b",
    flags=re.I,
)


def _clean_title(text: str, *, drop: tuple[str, ...] = ()) -> str:
    """Strip date/time noise (and event keywords) from a spoken title."""
    text = _STRIP_NOISE.sub(" ", text or "")
    for word in drop:
        text = re.sub(rf"\b{re.escape(word)}\b", " ", text, flags=re.I)
    # Trailing conjunctions left behind by a stripped clause ("… and tuesday"
    # -> "… , and") would otherwise end up in the title.
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[\s,;]*\b(?:and|then|also|plus|for|on|at|to|from)\b\s*$", "", text, flags=re.I)
    text = re.sub(r"[\s,;:]*\b(?:and|or)\s*[\s,;:]*$", "", text, flags=re.I)
    text = text.strip(" ,:-–—")
    text = re.sub(r"\s+([,.])", r"\1", text)
    text = re.sub(r",\s*,", ",", text)
    return text.strip(" ,:-–—")


_MUTATING = {"add_task", "add_event", "remember", "create_draft", "update_task", "complete_task"}


def _identity(name: str, result: dict) -> tuple | None:
    """What the tool actually created, so a re-stated request matches.

    A model asked to book "sunday, monday and tuesday" will often re-issue the
    same booking on every round of the tool loop, phrasing the dates
    differently each time. Keying on the created row rather than the arguments
    is what makes those collapse into one booking.
    """
    row = result.get(name.split("_", 1)[-1] if name.startswith("add_") else name)
    if name == "complete_task":
        row = result.get("task")
    if not isinstance(row, dict) or not row.get("id"):
        return None
    if name == "add_event":
        return (name, row.get("title"), row.get("start_at"), row.get("end_at"))
    if name == "add_task":
        return (name, (row.get("title") or "").strip().lower(), row.get("due_at"))
    if name == "remember":
        return (name, (row.get("text") or "").strip().lower())
    if name == "create_draft":
        return (name, row.get("audience"), row.get("subject"), (row.get("body") or "")[:80])
    return None


def _outcome(result: dict) -> dict:
    """Trim a tool result down to what an action record needs to carry."""
    out = {"message": result.get("message", "")[:200]}
    for key in ("task", "event", "draft", "memory"):
        row = result.get(key)
        if isinstance(row, dict) and row.get("id"):
            out[key] = {"id": row["id"], "title": row.get("title", "")}
            # Carry the resolved timestamps, not the model's phrasing: it asked
            # for "8:00 AM", the database knows which day that meant.
            for field in ("start_at", "end_at", "due_at"):
                if row.get(field):
                    out[key][field] = row[field]
    if result.get("warning"):
        out["warning"] = result["warning"]
    return out


def _sorted(value):
    """Order-insensitive view of a tool-args dict, for duplicate detection."""
    if isinstance(value, dict):
        return {k: _sorted(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_sorted(v) for v in value]
    return value


def _titlecase(text: str) -> str:
    return text[0].upper() + text[1:] if text else text


def clean_word(text: str) -> str:
    return (text or "").strip(" \t\n\r.,;:!?")


_KIND_WORDS = {
    "class": "class", "lecture": "class", "tutorial": "class", "tut": "class",
    "seminar": "class", "lab": "class", "exam": "deadline", "deadline": "deadline",
    "meeting": "meeting", "call": "meeting", "sync": "meeting", "standup": "meeting",
    "interview": "meeting", "study": "study", "session": "study", "review": "study",
    "revision": "study", "gym": "personal", "doctor": "personal", "dentist": "personal",
}


def _kind_from(word: str | None) -> str:
    if not word:
        return "class"
    key = word.lower()
    if key in _KIND_WORDS:
        return _KIND_WORDS[key]
    for needle, kind in _KIND_WORDS.items():
        if needle in key:
            return kind
    return "class"


# --------------------------------------------------------------------------- #
# OpenAI-compatible engine
# --------------------------------------------------------------------------- #
class OpenAIEngine:
    """Tool-calling engine against any OpenAI-compatible /chat/completions API.

    Works with OpenAI, Azure OpenAI, Groq, Together, OpenRouter, LM Studio,
    Ollama and friends — only the base URL and model change. Implemented with
    urllib so the app keeps its zero-dependency guarantee.
    """

    name = "openai"

    def __init__(self, store: Store, settings: LLMSettings | None = None):
        self.store = store
        self.settings = settings or load_llm_settings(store)
        self.fallback = LocalEngine(store)

    def available(self) -> bool:
        # Endpoints like Ollama and LM Studio have no real key. A stored
        # placeholder is enough to activate the engine, but an endpoint with
        # neither a key nor a keyless preset stays inactive.
        if self.settings.enabled:
            return True
        return not _requires_key(self.settings.base_url)

    def respond(self, message: str, history: list[dict] | None = None) -> Reply:
        if not self.available():
            return self.fallback.respond(message, history)

        last_exc: Exception | None = None
        chain = self._model_chain()
        for index, model in enumerate(chain):
            try:
                reply = self._chat(message, history or [], model=model)
            except Exception as exc:
                last_exc = exc
                if index == len(chain) - 1 or not _is_model_problem(exc):
                    break
                # Retired or overloaded model — pause briefly, then try the next.
                import time

                time.sleep(_MODEL_FALLBACK_BACKOFF)
                continue
            if index > 0:
                # Remember what actually worked so the next request starts there.
                self._remember_model(model)
                reply.note = f"switched to {model} (primary was unavailable)"
            return reply

        reply = self.fallback.respond(message, history)
        reply.degraded = True
        exc = last_exc
        # A programming error must never masquerade as a network blip — the
        # silent-fallback path is exactly what hid a real bug once already.
        if isinstance(exc, (NameError, AttributeError, TypeError, KeyError, IndexError)):
            reply.note = f"internal error in the {self.name} engine ({type(exc).__name__}: {exc})"
        elif exc is not None:
            reply.note = f"{self.name} unavailable ({_short_error(exc)}); answered locally."
        return reply

    def _remember_model(self, model: str) -> None:
        current = self.store.get_setting("llm", {}) or {}
        if current.get("model") != model:
            current["model"] = model
            self.store.set_setting("llm", current)

    # -- protocol -------------------------------------------------------- #
    def _post(self, payload: dict) -> dict:
        """One /chat/completions round-trip, with retry on transient failures.

        Free tiers are routinely rate-limited or overloaded; a single attempt
        turning into a silent downgrade to the local engine would be wrong, so
        back off and try again before giving up.
        """
        import time
        import urllib.error
        import urllib.request

        data = _dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"

        last: Exception | None = None
        for attempt in range(self.settings.retries):
            request = urllib.request.Request(
                f"{self.settings.base_url.rstrip('/')}/chat/completions",
                data=data, headers=headers,
            )
            try:
                with urllib.request.urlopen(request, timeout=self.settings.timeout) as response:
                    return _loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code not in _TRANSIENT_CODES or attempt == self.settings.retries - 1:
                    raise
                # Retrying a quota block is pointless when its reset window is
                # longer than the backoff we're willing to wait — it only burns
                # more of the quota we are already short on.
                if _wait_needed(exc) > _BACKOFF[min(attempt, len(_BACKOFF) - 1)]:
                    raise
            except Exception as exc:  # timeouts, resets, DNS blips
                last = exc
                if attempt == self.settings.retries - 1:
                    raise
            time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
        raise last if last else RuntimeError("request failed")

    def _model_chain(self) -> list[str]:
        """Configured model first, then same-provider fallbacks.

        Gemini's free tier retires model ids and intermittently returns 503, so
        having a second and third choice keeps the agent working unattended. A
        configured model the provider has since retired is dropped rather than
        tried first — it would only fail.
        """
        from ..config import resolve_preset

        preset = resolve_preset(self.store.get_setting("llm_preset"))
        retired = {m.lower() for m in (preset or {}).get("retired", [])}
        chain: list[str] = []
        if self.settings.model.lower() not in retired:
            chain.append(self.settings.model)
        if preset:
            for mid, _label in preset.get("models", []):
                if mid not in chain and mid.lower() not in retired:
                    chain.append(mid)
        if not chain:
            chain.append(self.settings.model)
        return chain

    def _chat(self, message: str, history: list[dict], model: str | None = None) -> Reply:
        from ..tools import REGISTRY

        model = model or self.settings.model
        messages: list[dict] = [{"role": "system", "content": build_system_prompt(self.store)}]
        for turn in history[-12:]:
            if turn.get("role") in {"user", "assistant"} and turn.get("content"):
                messages.append({"role": turn["role"], "content": str(turn["content"])[:4000]})
        messages.append({"role": "user", "content": message})

        tools = [t.schema() for t in REGISTRY if t.name != "delete_task"]
        actions: list[dict] = []
        reply_text = ""
        seen_calls: set[tuple] = set()
        created: set[tuple] = set()

        for _ in range(self.settings.max_tool_rounds):
            payload = {
                "model": model,
                "messages": messages,
                "temperature": self.settings.temperature,
                # Without an explicit budget, thinking models (Gemini 3.x) spend
                # the whole allowance on reasoning and return an empty reply.
                "max_tokens": self.settings.max_tokens,
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            body = self._post(payload)
            choice = (body.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            calls = msg.get("tool_calls") or []

            if not calls:
                reply_text = (msg.get("content") or "").strip()
                break

            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": calls,
            })

            for call in calls:
                fn = call.get("function") or {}
                name, raw_args = fn.get("name", ""), fn.get("arguments") or "{}"
                if isinstance(raw_args, dict):       # some providers send an object
                    args = raw_args
                else:
                    try:
                        args = _loads(raw_args) or {}
                    except ValueError:
                        args = {}
                if not isinstance(args, dict):
                    args = {}

                # Models sometimes emit the same call several times in one
                # batch (asking for "sunday, monday and tuesday" produced three
                # identical Sunday blocks). Collapse exact repeats so one
                # instruction can't quietly create five of something.
                signature = (name, _dumps(_sorted(args)))
                if name in _MUTATING and signature in seen_calls:
                    content = _dumps({"skipped": True,
                                      "why": "duplicate of an earlier call in this same message"})
                    outcome = {"skipped": True}
                else:
                    seen_calls.add(signature)
                    try:
                        result = run_tool(self.store, name, args)
                        content = _dumps(_slim(result))
                        outcome = _outcome(result)
                    except ToolError as exc:
                        content = _dumps({"error": str(exc)})
                        outcome = {"error": str(exc)}

                    # The raw arguments are not enough to spot a repeat: the
                    # model may express the same booking as "sunday" and later
                    # as "2026-09-27", which are one booking, not two. Compare
                    # what the tool actually created, across every round of the
                    # loop — otherwise a multi-round turn books it N times.
                    identity = _identity(name, result)
                    if identity and identity in created:
                        content = _dumps({
                            "skipped": True,
                            "why": "already created earlier in this same message",
                            "existing": content[:400],
                        })
                        outcome = {"skipped": True}
                    elif identity:
                        created.add(identity)
                # Record what actually happened, so callers (conflict checks,
                # the UI) can react to real results rather than intentions.
                actions.append({"tool": name, "args": args, "result": outcome})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": content[:8000],
                })
        else:
            reply_text = reply_text or (
                "Stopped after the tool-call limit — open the dashboard to see the current state.")

        reply_text = reply_text.strip() or self.fallback.respond(message).text
        return Reply(reply_text, actions=actions, engine=self.name)


# Free-tier providers routinely return 429/503. These are worth retrying; a 400
# or 401 is not, because retrying a rejected request only wastes quota.
_TRANSIENT_CODES = {408, 409, 429, 500, 502, 503, 504}
_BACKOFF = (1.0, 2.5, 5.0)
# Seconds to wait between model attempts when one model is unusable.
_MODEL_FALLBACK_BACKOFF = 0.4


def _error_text(exc) -> str:
    import urllib.error

    if not isinstance(exc, urllib.error.HTTPError):
        return str(exc)
    try:
        raw = _loads(exc.read().decode())
    except Exception:
        return getattr(exc, "reason", "") or ""
    payload = raw[0] if isinstance(raw, list) and raw else raw
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return error.get("message") or error.get("status") or ""
    return str(payload)[:200]


def _wait_needed(exc) -> float:
    """Seconds the provider says to wait before retrying, if it says so."""
    import re as _re

    match = _re.search(r"retry in ([\d.]+)\s*s", _error_text(exc), _re.I)
    return float(match.group(1)) if match else 0.0


def _short_error(exc) -> str:
    text = _error_text(exc)
    if not text:
        return type(exc).__name__
    # A quota message is long and link-heavy; the useful part is the reset time.
    import re as _re

    retry = _re.search(r"retry in ([\d.]+)\s*s", text, _re.I)
    if retry:
        return f"free-tier rate limit — retry in about {int(float(retry.group(1)))}s"
    if "quota" in text.lower():
        return "free-tier quota exhausted"
    return text[:70]


def _is_model_problem(exc) -> bool:
    """True when a different model id might succeed."""
    import urllib.error

    if not isinstance(exc, urllib.error.HTTPError):
        return False
    if exc.code == 404:
        return True
    if exc.code not in _TRANSIENT_CODES:
        return False
    text = _error_text(exc).lower()
    return any(w in text for w in ("model", "demand", "unavailable", "overloaded", "capacity"))


def _dumps(value) -> str:
    import json

    return json.dumps(value, default=str)


def _requires_key(base_url: str) -> bool:
    """False for keyless local endpoints (Ollama, LM Studio, vLLM)."""
    url = (base_url or "").lower()
    return not any(host in url for host in (
        "localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal", "lmstudio", "ollama"))


def diagnose(store: Store, settings: LLMSettings | None = None) -> dict:
    """Verify a provider end-to-end: reachability, auth, and tool-calling.

    Tool-calling is checked separately because a model can answer perfectly
    while being unable to act — and that is the failure mode that actually
    matters for this app.
    """
    import time
    import urllib.error
    import urllib.request

    from ..config import resolve_preset
    from ..tools import REGISTRY

    settings = settings or load_llm_settings(store)
    url = f"{settings.base_url.rstrip('/')}/chat/completions"
    result: dict = {
        "ok": False, "base_url": settings.base_url, "model": settings.model,
        "key_set": settings.enabled, "reachable": False,
        "auth_ok": False, "tools_ok": False, "latency_ms": None, "error": None,
    }
    if not settings.enabled and _requires_key(settings.base_url):
        result["error"] = "No API key set. Add one in Settings, or switch provider."
        return result

    def call(payload, timeout=None):
        # Reuse the engine's retrying transport so a transient 429/503 during
        # the check doesn't get misreported as a missing capability.
        engine = OpenAIEngine(store, settings)
        started = time.monotonic()
        body = engine._post(payload)
        return body, int((time.monotonic() - started) * 1000)

    # 1. reachability + auth + a real completion
    try:
        body, ms = call({
            "model": settings.model,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "max_tokens": settings.max_tokens, "temperature": 0,
        })
        result["reachable"] = True
        result["auth_ok"] = True
        result["latency_ms"] = ms
        text = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        result["sample"] = text.strip()[:80]
        result["ok"] = True
        if not text.strip():
            # Authenticated fine but spent the budget reasoning. Not fatal, but
            # worth surfacing: it means replies may come back thin.
            result["warning"] = (
                f"{settings.model} returned an empty reply — it is a thinking model and may "
                "need a larger response budget."
            )
    except urllib.error.HTTPError as exc:
        result["error"] = _explain_http(exc, settings)
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    # 2. tool-calling, which is what makes the agent able to act
    try:
        probe = {
            "name": "add_task", "description": "Create a task.",
            "parameters": {"type": "object", "properties": {"title": {"type": "string"}},
                           "required": ["title"]},
        }
        body, _ = call({
            "model": settings.model,
            "messages": [{"role": "user", "content": "Add a task titled 'connection probe'"}],
            "tools": [{"type": "function", "function": probe}],
            "tool_choice": "auto", "max_tokens": settings.max_tokens,
        })
        calls = ((body.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or []
        result["tools_ok"] = bool(calls)
        if calls:
            result["tool_probe"] = (calls[0].get("function") or {}).get("name")
    except urllib.error.HTTPError as exc:
        # A rate limit is not a missing capability — say which it was.
        if exc.code in _TRANSIENT_CODES:
            result["tool_error"] = ("Rate limited or temporarily unavailable "
                                    f"(HTTP {exc.code}) — the agent will retry on its own. "
                                    "Tool-calling is untested this run.")
        else:
            result["tool_error"] = _explain_http(exc, settings)
    except Exception as exc:
        result["tool_error"] = f"{type(exc).__name__}: {exc}"

    preset = resolve_preset(store.get_setting("llm_preset"))
    if preset:
        result["note"] = preset.get("note")
    return result


def _loads(text: str):
    import json

    return json.loads(text)


def _explain_http(exc, settings) -> str:
    """Turn provider-specific status codes into something actionable."""
    detail = ""
    try:
        body = _loads(exc.read().decode())
        # Google wraps the error object in a one-element list; other
        # OpenAI-compatible providers return {"error": {...}} bare. Accept both,
        # and never fall back to dumping raw JSON at the user.
        payload = body
        if isinstance(payload, list) and payload:
            payload = payload[0]
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, list) and error:
            error = error[0]
        if isinstance(error, dict):
            detail = error.get("message") or error.get("status") or ""
        if not detail and isinstance(payload, dict):
            detail = payload.get("message") or ""
        if not detail:
            detail = str(getattr(exc, "reason", "") or "no detail provided")
    except Exception:
        detail = getattr(exc, "reason", "") or "no detail provided"

    code = exc.code
    low = (detail or "").lower()

    # A 400 that mentions the key is an auth problem, not a bad request —
    # Gemini's shim reports invalid keys that way, which is easy to misread.
    if code == 400 and ("api key" in low or "api_key" in low or "unauthenticated" in low):
        return f"Invalid API key — {detail}"
    if code in (401, 403) and ("key" in low or code == 401):
        return f"Key rejected — {detail or 'check the key and its enabled APIs'}"
    # A retired or unavailable model is the most common misconfiguration, and
    # providers phrase it as a 404. Say exactly that.
    if code == 404 and ("no longer available" in low or "not found" in low
                        or "is not supported" in low or "unsupported" in low):
        replacement = ""
        match = re.search(r"use (models/[A-Za-z0-9._-]+)", detail)
        if match:
            replacement = f" Use {match.group(1).split('/')[-1]} instead."
        return f"Model '{settings.model}' isn't available on this key.{replacement} Pick a different model in Settings."

    hint = {
        400: "Request rejected — check the model id and base URL.",
        403: "Key rejected, or this model isn't available on your tier.",
        404: "Wrong base URL or model id. For Gemini use "
             "https://generativelanguage.googleapis.com/v1beta/openai",
        429: "Free-tier rate limit hit — this is normal on the free plan, "
             "just wait a moment and retry.",
    }.get(code, "")
    if code == 429 and "quota" in low:
        import re as _re
        retry = _re.search(r"retry in ([\d.]+)\s*s", detail, _re.I)
        when = f" Resets in about {int(float(retry.group(1)))}s." if retry else ""
        return f"Free-tier quota exhausted.{when} Local answers still work."
    return f"HTTP {code}: {detail[:160]}" + (f"  ({hint})" if hint else "")


def _slim(result: dict) -> dict:
    """Trim bulky tool output so it stays inside the context window."""
    out = {}
    for key, value in result.items():
        if key in {"tasks", "events", "memory", "drafts", "conflicts", "slots"}:
            out[key] = [
                {k: v2 for k, v2 in item.items() if k in {
                    "id", "title", "status", "priority_label", "due_at", "due_human",
                    "label", "day_label", "kind", "text", "subject", "audience", "overdue",
                }}
                for item in value[:25]
            ]
        elif key == "overview":
            out[key] = {
                "counts": value.get("counts"),
                "today": [e.get("title") for e in value.get("today", [])][:10],
                "conflicts": len(value.get("conflicts", [])),
            }
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------- #
# hybrid router
# --------------------------------------------------------------------------- #
# Signals that a request genuinely needs model reasoning rather than pattern
# matching. Kept deliberately narrow: the common capture/lookup phrasings must
# stay local so they cost zero tokens and return instantly.
_NEEDS_REASONING = re.compile(
    r"\b(why|how come|explain|compare|contrast|summari[sz]e|triage|prioriti[sz]e|"
    r"plan|break down|strategy|approach|pros|cons|trade.?off|analy[sz]e|evaluate|"
    r"rewrite|proofread|improve|suggest|recommend|decide|between)\b", re.I)
_META = re.compile(r"\b(who|what|when|where|which)\b.{0,40}\?", re.I)

_DAY_REF = re.compile(
    r"\b(today|tomorrow|yesterday|tonight|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|"
    r"jul|aug|sep|oct|nov|dec)[a-z]*)\b", re.I)
_CLOCK_REF = re.compile(r"\b\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?\b|\b\d{1,2}:\d{2}\b", re.I)


def _needs_cloud(message: str) -> tuple[bool, str]:
    """Deterministic pre-flight. Returns (escalate, reason)."""
    text = (message or "").strip()
    if not text:
        return False, ""
    if _NEEDS_REASONING.search(text):
        return True, "asks for reasoning"
    if "?" in text:
        return True, "is a question"
    # A multi-day booking ("sunday, monday and tuesday") needs real date
    # arithmetic. A pattern matcher will happily collapse it to one wrong day.
    days = {m.group(0).lower() for m in _DAY_REF.finditer(text)}
    if len(days) > 1:
        return True, f"spans several dates ({', '.join(sorted(days))})"
    if len(_CLOCK_REF.findall(text)) > 1 and re.search(
            r"\b(and|then|also|,)\b", text, re.I):
        return True, "several time ranges in one request"
    if len(text) > 240 or text.count("\n") >= 2:
        return True, "multi-part request"
    return False, ""


class HybridEngine:
    """Local by default, cloud only when the local engine can't or shouldn't.

    Order of attempts:
      1. obvious reasoning request  -> cloud immediately (skip the local guess)
      2. local engine handles it    -> done, zero latency, zero tokens
      3. local engine defers        -> cloud
      4. cloud unavailable or fails -> local, flagged as degraded
    """

    name = "hybrid"

    def __init__(self, store: Store, cloud: Engine | None = None):
        self.store = store
        self.local = LocalEngine(store)
        self.cloud = cloud or OpenAIEngine(store)
        self.last_reason = ""

    def available(self) -> bool:
        return True

    def respond(self, message: str, history: list[dict] | None = None) -> Reply:
        escalate, reason = _needs_cloud(message)
        if escalate:
            reply = self._ask_cloud(message, history)
            if reply is not None:
                self.last_reason = reason
                reply.note = self.last_reason
                return reply

        reply = self.local.respond(message, history)
        if not reply.deferred:
            self.last_reason = "handled locally"
            return reply

        cloud_reply = self._ask_cloud(message, history)
        if cloud_reply is not None:
            self.last_reason = "local could not handle it"
            cloud_reply.note = self.last_reason
            return cloud_reply

        return reply

    def _ask_cloud(self, message: str, history: list[dict] | None) -> Reply | None:
        if not self.cloud.available():
            return None
        reply = self.cloud.respond(message, history)
        if reply.degraded:
            return None
        return reply


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #
def get_engine(store: Store, provider: str | None = None) -> Engine:
    """Resolve the active engine.

    ``provider`` = local | hybrid (default) | openai | auto. A cloud engine is
    considered available when it has a key *or* when the endpoint is a keyless
    local one (Ollama, LM Studio) — so a fresh install needs no configuration,
    yet those endpoints still switch on.
    """
    provider = (provider or store.get_setting("provider", "hybrid") or "hybrid").lower()
    llm = load_llm_settings(store)
    cloud = OpenAIEngine(store, llm)
    cloud_ready = cloud.available()

    if provider == "local" or not cloud_ready:
        return LocalEngine(store)
    if provider == "openai":
        return cloud
    return HybridEngine(store, cloud)
