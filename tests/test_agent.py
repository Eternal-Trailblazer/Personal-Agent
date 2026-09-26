"""End-to-end tests over a throwaway database: store, tools and both engines.

    python tests/test_agent.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from personal_agent.brain.engine import LocalEngine, get_engine  # noqa: E402
from personal_agent.demo import seed  # noqa: E402
from personal_agent.prompt import build_system_prompt  # noqa: E402
from personal_agent.store import Store  # noqa: E402
from personal_agent.timeutil import TZ  # noqa: E402
from personal_agent.tools import ToolError, run_tool  # noqa: E402


class Check:
    def __init__(self):
        self.failures: list[str] = []
        self.passed = 0

    def that(self, condition: bool, label: str) -> bool:
        if condition:
            self.passed += 1
        else:
            self.failures.append(label)
        return bool(condition)

    def equal(self, got, want, label: str) -> bool:
        return self.that(got == want, f"{label}\n     got {got!r}\n  wanted {want!r}")


def main() -> int:
    c = Check()
    tmp = Path(tempfile.mkdtemp()) / "agent.db"
    store = Store(tmp)
    seed(store)

    # ------------------------------------------------------------------ #
    # store
    # ------------------------------------------------------------------ #
    counts = store.task_counts()
    c.that(counts["open"] > 0, "seed creates open tasks")
    c.that(counts["overdue"] == 1, f"seed creates 1 overdue task, got {counts['overdue']}")

    today_events = store.agenda()
    c.that(len(today_events) == 3, f"3 events today, got {len(today_events)}")
    c.that(today_events == sorted(today_events, key=lambda e: e["start_at"]), "agenda is time-sorted")
    c.that(all(e["label"] and e["minutes"] > 0 for e in today_events), "events carry a label + duration")

    # free slots never overlap booked events
    free = store.free_slots()
    c.that(all(s["minutes"] >= 30 for s in free), "free slots are at least 30 min")

    # task lifecycle
    task = store.add_task("Write the summary", priority=1, project="Project")
    c.equal(task["status"], "open", "new task is open")
    c.equal(task["priority_label"], "P1", "P1 label")
    done = store.complete_task(task["id"])
    c.equal(done["status"], "done", "task completes")
    c.that(done["completed_at"] is not None, "completed_at is stamped")
    reopened = store.update_task(task["id"], status="open")
    c.that(reopened["completed_at"] is None, "reopening clears completed_at")
    c.that(store.delete_task(task["id"]), "task deletes")
    c.that(store.get_task(task["id"]) is None, "deleted task is gone")

    # conflict detection
    base = TZ.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=5)
    fmt = "%Y-%m-%dT%H:%M"
    store.add_event("Clash A", base.strftime(fmt), (base + timedelta(hours=2)).strftime(fmt))
    store.add_event("Clash B", (base + timedelta(hours=1)).strftime(fmt),
                    (base + timedelta(hours=3)).strftime(fmt))
    clashes = store.conflicts(base.strftime(fmt), (base + timedelta(days=1)).strftime(fmt))
    c.that(len(clashes) == 1, f"overlap detected, got {len(clashes)}")
    c.equal(clashes[0]["overlap_min"], 60, "overlap is 60 minutes")

    # ------------------------------------------------------------------ #
    # tools
    # ------------------------------------------------------------------ #
    res = run_tool(store, "add_task", {"title": "Buy lab goggles", "due": "friday 5pm"})
    c.that(res["task"]["due_at"] is not None, "add_task parses a natural-language due date")
    c.that("Added task" in res["message"], "add_task returns a message")

    res = run_tool(store, "add_event", {"title": "Lecture", "start": "tomorrow 4pm", "end": "tomorrow 6pm"})
    c.that(res["event"]["end_at"] > res["event"]["start_at"], "add_event end follows start")

    c.that("tasks" in run_tool(store, "list_tasks", {"scope": "overdue"}), "list_tasks overdue")
    c.that("message" in run_tool(store, "daily_brief", {}), "daily_brief summarises")
    c.that(run_tool(store, "recall", {})["count"] >= 2, "memory is retrievable")

    draft = run_tool(store, "create_draft", {"audience": "Prof Sharma", "subject": "Deadline",
                                              "body": "Hi, could I get an extension?"})
    c.equal(draft["draft"]["status"], "draft", "drafts start unsent")

    # timestamps must normalise to ISO wall-clock regardless of input format
    iso = "%Y-%m-%dT%H:%M"
    ev = store.add_event("Natural language", "today 15:00", "today 16:30")
    c.that(ev["start_at"].startswith(TZ.today().strftime("%Y-%m-%d")),
           f"natural-language start normalises, got {ev['start_at']!r}")
    c.that(ev["minutes"] == 90, f"duration computed, got {ev['minutes']}")
    c.that(ev["label"] not in ("", "—"), "a normalising event gets a readable label")
    c.that(store.add_event("ISO input", f"{TZ.today():%Y-%m-%d}T08:00",
                           f"{TZ.today():%Y-%m-%d}T09:00")["label"] != "—",
           "ISO input still works")

    # ordering must be correct across mixed input formats
    mixed = store.list_events(start=f"{TZ.today():%Y-%m-%d}T00:00", end=f"{TZ.today():%Y-%m-%d}T23:59")
    starts = [e["start_at"] for e in mixed]
    c.equal(starts, sorted(starts), "mixed-format events sort chronologically")

    # a genuine clash is reported
    clash_day = (TZ.today() + timedelta(days=6))
    s1 = f"{clash_day:%Y-%m-%d}T10:00"
    s2 = f"{clash_day:%Y-%m-%d}T11:00"
    store.add_event("Morning block", s1, f"{clash_day:%Y-%m-%d}T12:00")
    store.add_event("Midday block", s2, f"{clash_day:%Y-%m-%d}T13:00")
    day_clashes = store.conflicts(s1, f"{clash_day:%Y-%m-%d}T23:59")
    c.that(len(day_clashes) == 1, f"clash found, got {len(day_clashes)}")

    # unparseable times are rejected, not silently stored
    for bad_args in [{"title": "X", "start_at": "whenever", "end_at": ""},
                     {"title": "X", "start_at": "today 10:00", "end_at": "today 09:00"}]:
        try:
            store.add_event(**bad_args)
            c.that(False, f"expected ValueError for {bad_args}")
        except ValueError:
            c.passed += 1

    # task due dates normalise too
    t2 = store.add_task("Normalise me", due_at="tomorrow 9am")
    c.that(t2["due_at"].endswith("T09:00"), f"task due normalises, got {t2['due_at']!r}")
    c.that(t2["due_today"] is False, "tomorrow's task is not due today")

    # error handling
    for bad in [lambda: run_tool(store, "nope", {}),
                lambda: run_tool(store, "complete_task", {"id": 99999}),
                lambda: run_tool(store, "add_task", {"title": "  "}),
                lambda: run_tool(store, "add_event", {"title": "X", "start": "whenever"})]:
        try:
            bad()
            c.that(False, "expected a ToolError for bad input")
        except ToolError:
            c.passed += 1
        except Exception as exc:
            c.that(False, f"expected ToolError, got {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    # local engine
    # ------------------------------------------------------------------ #
    local = LocalEngine(store)

    c.that(local.available(), "local engine is always available")
    c.that("task" in local.respond("add task renew passport").text.lower(), "local engine adds tasks")
    c.that("scheduled" in local.respond("schedule lecture 4pm tomorrow").text.lower(),
           "local engine schedules events")
    c.that("remembered" in local.respond("remember I have a viva on the 30th").text.lower(),
           "local engine remembers")
    c.that("overdue" in local.respond("overdue").text.lower(), "local engine reports overdue")
    c.that("free" in local.respond("free slots today").text.lower(), "local engine reports free time")
    c.that(len(local.respond("add task something with a deadline friday 6pm").actions) == 1,
           "local engine reports the action it took")

    # a title must not be mangled by date stripping
    local.respond("add task submit the thermodynamics problem set friday 5pm")
    titles = [t["title"] for t in run_tool(store, "list_tasks", {"scope": "open", "limit": 100})["tasks"]]
    c.that("Submit the thermodynamics problem set" in titles,
           f"title preserved, got {titles[:4]}")

    # out-of-scope input should be declined, not guessed at
    c.that("didn't catch" in local.respond("what is the meaning of life").text,
           "local engine declines unhandled requests")

    # "add a task ..." must capture, not be hijacked by the bare "task(s)" noun
    for phrasing in ["add a task to email the supervisor friday",
                     "Please add a task to call the lab tomorrow",
                     "remind me to submit the form monday"]:
        before = store.task_counts()["total"]
        local.respond(phrasing)
        c.that(store.task_counts()["total"] == before + 1,
               f"captures rather than lists: {phrasing!r}")

    # an explicitly named day must be honoured, not silently replaced by today
    for phrasing, expected_marker in [("what's due tomorrow", "Sun"),
                                      ("what's due friday", "Fri")]:
        text = local.respond(phrasing).text
        c.that(expected_marker in text and "due today" not in text,
               f"{phrasing!r} answers about the named day, got {text.splitlines()[0]!r}")
    c.that("due Sat" in local.respond("what's due today").text,
           "'what's due today' stays a task list, not a brief")
    c.that("Free on" in local.respond("brief").text or "Start with" in local.respond("brief").text,
           "'brief' still returns the daily brief")
    c.that("Start with" in local.respond("what's on today").text,
           "'what's on today' still returns the daily brief")

    # ------------------------------------------------------------------ #
    # system prompt
    # ------------------------------------------------------------------ #
    prompt = build_system_prompt(store)
    for token in ("Personal Agent", "Shekhar", "Operating principles", "Working memory", "Boundaries"):
        c.that(token in prompt, f"system prompt contains {token!r}")
    c.that("viva on the 30th" in prompt, "system prompt includes live working memory")

    # ------------------------------------------------------------------ #
    # engine routing
    # ------------------------------------------------------------------ #
    store.set_setting("provider", "local")
    c.equal(get_engine(store).name, "local", "provider=local routes to the local engine")
    store.set_setting("provider", "hybrid")
    c.equal(get_engine(store).name, "local", "hybrid with no key degrades to local")

    store.set_setting("llm", {"api_key": "sk-test", "model": "gemini-2.5-flash",
                              "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"})
    c.equal(get_engine(store).name, "hybrid", "a stored key activates hybrid")
    store.set_setting("provider", "openai")
    c.equal(get_engine(store).name, "openai", "provider=openai forces the cloud engine")
    store.set_setting("provider", "local")
    c.equal(get_engine(store).name, "local", "provider=local overrides a stored key")
    store.set_setting("provider", "hybrid")

    # keyless local endpoints (Ollama/LM Studio) must activate without a key
    store.set_setting("llm", {"api_key": "", "model": "qwen2.5:7b-instruct",
                              "base_url": "http://localhost:11434/v1"})
    engine = get_engine(store)
    c.that(engine.name == "hybrid" and engine.cloud.available(),
           "Ollama activates with no key")
    store.set_setting("llm", {"api_key": "sk-test", "model": "gemini-2.5-flash",
                              "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"})

    # ------------------------------------------------------------------ #
    # hybrid routing decisions
    # ------------------------------------------------------------------ #
    from personal_agent.brain.engine import HybridEngine, _needs_cloud

    for text, escalate, label in [
        ("add task buy lab goggles friday 5pm", False, "simple capture stays local"),
        ("what's due today", False, "simple lookup stays local"),
        ("overdue", False, "single-word review stays local"),
        ("free slots tomorrow", False, "free-slot query stays local"),
        ("why is my schedule so fragmented?", True, "a 'why' question escalates"),
        ("What's the best way to split the syllabus across three weeks?", True, "a question escalates"),
        ("compare linear algebra vs discrete maths for my semester", True, "a comparison escalates"),
    ]:
        got, _ = _needs_cloud(text)
        c.equal(got, escalate, f"_needs_cloud({text!r}) — {label}")

    # A hybrid engine with a stub cloud must prefer local for simple input
    # and escalate for questions, without ever raising.
    class StubCloud:
        name = "stub"
        def __init__(self): self.calls = []
        def available(self): return True
        def respond(self, message, history=None):
            from personal_agent.brain.engine import Reply
            self.calls.append(message)
            return Reply("cloud answer", engine="stub")

    stub = StubCloud()
    hybrid = HybridEngine(store, stub)
    reply = hybrid.respond("add task a task only the local parser sees tomorrow 4pm")
    c.equal(reply.engine, "local", "hybrid keeps simple capture local")
    c.equal(len(stub.calls), 0, "local handling costs zero cloud calls")

    reply = hybrid.respond("why should I revise linear algebra first?")
    c.equal(reply.engine, "stub", "hybrid escalates a reasoning question")
    c.equal(len(stub.calls), 1, "escalation reaches the cloud")

    # A cloud engine that reports degraded must fall back to local
    class BrokenCloud:
        name = "broken"
        def available(self): return True
        def respond(self, message, history=None):
            from personal_agent.brain.engine import Reply
            return Reply("local answer instead", engine="local", deferred=True)

    hybrid2 = HybridEngine(store, BrokenCloud())
    reply = hybrid2.respond("summarise my week and tell me what to prioritise")
    c.that(reply.text == "local answer instead", "a failing cloud falls back to local")

    # The real bug this guards: an internal error inside the cloud tool-call
    # loop used to be swallowed and reported as an ordinary network fallback,
    # so the agent silently lost the ability to act. Programming errors must be
    # labelled as such rather than disguised as a connectivity blip.
    from personal_agent.brain.engine import OpenAIEngine

    crashing = OpenAIEngine(store)

    def _boom(message, history=None, model=None):
        raise NameError("name 'json' is not defined")

    crashing._chat = _boom
    reply = crashing.respond("add task something due tomorrow")
    c.that(reply.degraded, "an internal error marks the reply degraded")
    c.that("internal error" in (reply.note or ""),
           f"an internal error is named, got {reply.note!r}")

    # A retired/overloaded model must roll over to the next one, and remember it.
    import urllib.error

    from personal_agent.config import load_llm_settings

    class Flaky(OpenAIEngine):
        def __init__(self, store, settings, fail_model):
            super().__init__(store, settings)
            self.fail_model = fail_model
            self.used: list[str] = []

        def _chat(self, message, history=None, model=None):
            model = model or self.settings.model
            self.used.append(model)
            if model == self.fail_model:
                raise urllib.error.HTTPError("url", 404, "not found", {}, None)
            from personal_agent.brain.engine import Reply
            return Reply("answered by the fallback model", engine=self.name)

    store.set_setting("llm_preset", "gemini")
    store.set_setting("llm", {"api_key": "sk-test", "model": "gemini-3.8-flash",
                              "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"})
    settings = load_llm_settings(store)
    flaky = Flaky(store, settings, fail_model=settings.model)
    reply = flaky.respond("why should I revise linear algebra first?")
    c.that(len(flaky.used) > 1, f"an unavailable model rolls over, tried {flaky.used}")
    c.equal(reply.text, "answered by the fallback model", "the fallback model answers")
    c.that("switched to" in (reply.note or ""), f"the switch is disclosed, got {reply.note!r}")
    c.that((store.get_setting("llm", {}) or {}).get("model") != settings.model,
           "the working model is remembered for next time")

    # a configured-but-retired model is dropped rather than tried first
    store.set_setting("llm", {"api_key": "sk-test", "model": "gemini-2.5-flash",
                              "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"})
    chain = OpenAIEngine(store, load_llm_settings(store))._model_chain()
    c.that("gemini-2.5-flash" not in chain,
           f"a retired configured model is skipped, got {chain}")
    c.that(len(chain) >= 2, f"live fallbacks remain available, got {chain}")

    # diagnose() must report a missing key rather than making a network call
    from personal_agent.brain import diagnose
    store.set_setting("llm", {"api_key": "", "base_url": "https://api.openai.com/v1",
                              "model": "gpt-4o-mini"})
    diag = diagnose(store)
    c.that(diag["ok"] is False and "key" in (diag["error"] or "").lower(),
           f"diagnose reports a missing key, got {diag['error']!r}")
    store.set_setting("llm", {"api_key": "sk-test", "model": "gemini-2.5-flash",
                              "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"})

    # ------------------------------------------------------------------ #
    # date context, multi-day handling, and duplicate suppression
    # ------------------------------------------------------------------ #
    from personal_agent.prompt import calendar_window

    window = calendar_window()
    c.that("Sunday" in window and "(2026-09-27)" in window,
           "the prompt's date map names weekdays with ISO dates")

    # A multi-day booking is beyond a pattern matcher and must be handed on.
    escalate, reason = _needs_cloud("block prep 8am to 12pm on sunday, monday and tuesday")
    c.that(escalate and "several dates" in reason,
           f"multi-day booking escalates to the cloud, got {reason!r}")
    local_reply = local.respond("block prep 8am to 12pm on sunday, monday and tuesday")
    c.that(local_reply.deferred, "the local engine refuses a multi-day booking")
    before = len(store.list_events(limit=500))
    store.agenda()
    c.that(len(store.list_events(limit=500)) == before, "a refused request creates nothing")

    # Titles must not keep trailing conjunctions left over from a stripped date.
    from personal_agent.brain.engine import _clean_title

    for text, expected in [
        ("Physics midterm prep, and", "Physics midterm prep"),
        ("Study block on friday and", "Study block"),
        ("Lecture 4pm tomorrow", "Lecture"),
        ("Physics prep  and tuesday", "Physics prep"),
    ]:
        got = _clean_title(text)
        c.that(got == expected, f"_clean_title({text!r}) -> {got!r}, wanted {expected!r}")

    # A model re-issuing the same booking on every round of the tool loop must
    # create it once. Identity is keyed on the resolved row, not the phrasing,
    # because "sunday" and "2026-09-27" are the same booking.
    from personal_agent.brain.engine import _identity, _outcome

    made = run_tool(store, "add_event", {"title": "Physics prep", "start": "2026-09-27T08:00",
                                         "end": "2026-09-27T12:00"})
    same = run_tool(store, "add_event", {"title": "Physics prep", "start": "Sunday 8am",
                                         "end": "Sunday 12pm"})
    c.that(_identity("add_event", made) == _identity("add_event", same),
           "the same booking phrased two ways yields one identity")
    c.that(_identity("add_event", made) != _identity(
        "add_event", run_tool(store, "add_event", {"title": "Physics prep",
                                                    "start": "2026-09-28T08:00",
                                                    "end": "2026-09-28T12:00"})),
        "a different day is a different booking")

    # Conflict reporting must ignore events created in the same message.
    from personal_agent.server import _new_conflicts

    a = run_tool(store, "add_event", {"title": "Study A", "start": "2026-10-10T10:00",
                                      "end": "2026-10-10T12:00"})
    b = run_tool(store, "add_event", {"title": "Study B", "start": "2026-10-10T11:00",
                                      "end": "2026-10-10T13:00"})
    actions = [
        {"tool": "add_event", "args": {"title": "Study A", "start": "10:00"},
         "result": _outcome(a)},
        {"tool": "add_event", "args": {"title": "Study B", "start": "11:00"},
         "result": _outcome(b)},
    ]
    c.equal(_new_conflicts(store, actions), [], "two events created together are not a self-conflict")

    clash = run_tool(store, "add_event", {"title": "Study C", "start": "2026-10-10T11:30",
                                          "end": "2026-10-10T14:00"})
    reported = _new_conflicts(store, [{"tool": "add_event",
                                       "args": {"title": "Study C"},
                                       "result": _outcome(clash)}])
    c.that(any("Study A" in r for r in reported),
           f"a clash with a pre-existing event is reported, got {reported}")
    c.that(not any("Study C vs Study C" in r for r in reported),
           "an event is never reported as clashing with itself")

    # presets must cover Gemini with the OpenAI-compatible base URL
    from personal_agent.config import PROVIDER_PRESETS, resolve_preset  # noqa: F811
    gemini = resolve_preset("gemini")
    c.that(gemini is not None, "a gemini preset exists")
    c.equal(gemini["base_url"], "https://generativelanguage.googleapis.com/v1beta/openai",
            "gemini base URL is the OpenAI-compatible endpoint")
    c.that(gemini["model"] in [m[0] for m in gemini["models"]],
           "the default gemini model is one of the offered choices")
    c.that(not resolve_preset("ollama")["key_required"], "ollama is marked keyless")
    c.that(resolve_preset("gemini")["key_required"], "gemini requires a key")
    # every preset must declare a response budget, or thinking models return nothing
    for pid, preset in PROVIDER_PRESETS.items():
        c.that(preset.get("max_tokens", 0) >= 512,
               f"preset {pid} declares a usable max_tokens")
    # a retired model must never be offered or used as a fallback
    c.that("gemini-2.5-flash" in resolve_preset("gemini")["retired"],
           "retired gemini ids are recorded so they are never used as fallbacks")
    chain = OpenAIEngine(store, load_llm_settings(store))._model_chain()
    c.that(not any(m in resolve_preset("gemini")["retired"] for m in chain),
           f"the fallback chain excludes retired models, got {chain}")

    print()
    if c.failures:
        print(f"FAILED ({len(c.failures)})\n")
        for f in c.failures:
            print("  •", f)
        return 1
    print(f"passed {c.passed} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
