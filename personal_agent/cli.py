"""Terminal interface — a local-first REPL over the same engine and tools."""

from __future__ import annotations

import shlex
import sys

from .brain import get_engine
from .config import APP_NAME, VERSION
from .server import ROUTES, Handler, _free_port
from .store import Store
from .timeutil import TZ, fmt_range
from .tools import ToolError, run_tool

C = {
    "reset": "\033[0m", "dim": "\033[2m", "b": "\033[1m",
    "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m",
    "red": "\033[31m", "grey": "\033[90m",
}


def _c(text: str, *styles: str) -> str:
    if not sys.stdout.isatty() or "NO_COLOR" in __import__("os").environ:
        return text
    return "".join(C[s] for s in styles) + text + C["reset"]


BANNER = f"""
  {_c('PERSONAL AGENT', 'b', 'cyan')}  {_c(f'v{VERSION}  ·  {TZ.name}  ·  local-first', 'dim')}
  Type naturally.  /help for commands.  /quit to exit.
"""


def cmd_help() -> str:
    return "\n".join([
        _c("Conversation", "b"),
        "  <anything>            talk to the active engine",
        "",
        _c("Quick capture", "b"),
        "  task <title> [due]    add a task, e.g. task submit assignment fri 5pm",
        "  sched <title> <when>  block the calendar, e.g. sched lecture tomorrow 4pm",
        "  note <text>           remember a durable fact",
        "",
        _c("Review", "b"),
        "  today | overdue | conflicts | free | brief | tasks | drafts",
        "",
        _c("Tools (raw)", "b"),
        "  tool <name> k=v ...   e.g. tool add_task title=\"Lab report\" due=\"mon\"",
        "  tools                 list every tool",
        "",
        _c("System", "b"),
        "  /provider local|openai|auto   switch intelligence layer",
        "  /config                        show active configuration",
        "  /serve [port]                  start the dashboard",
        "  /clear                         clear screen",
        "  /quit                          exit",
    ])


def run(store: Store) -> int:
    print(BANNER)
    engine = get_engine(store)
    history: list[dict] = []

    def refresh_engine():
        nonlocal engine
        engine = get_engine(store)

    while True:
        try:
            raw = input(_c("you › ", "cyan")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Bye.")
            return 0
        if not raw:
            continue

        if raw in {"/quit", "/exit", "quit", "exit"}:
            print("  Bye.")
            return 0
        if raw == "/help":
            print(cmd_help())
            continue
        if raw == "/clear":
            print("\033[2J\033[H", end="")
            continue
        if raw == "/tools":
            from .tools import REGISTRY
            for t in REGISTRY:
                print(f"  {_c(t.name.ljust(15), 'cyan')} {_c(t.description, 'dim')}")
            continue
        if raw == "/config":
            print(f"  engine   {engine.name}   llm={engine.available()}")
            print(f"  db       {store.path}")
            print(f"  tz       {TZ.name} ({TZ.offset_label()})")
            print(f"  tasks    {store.task_counts()}")
            continue
        if raw.startswith("/provider"):
            parts = raw.split()
            if len(parts) > 1:
                store.set_setting("provider", parts[1])
                refresh_engine()
            print(f"  engine → {engine.name}")
            continue
        if raw.startswith("/serve"):
            parts = raw.split()
            port = int(parts[1]) if len(parts) > 1 else 8765
            from .server import serve
            httpd = serve(store, port=port, open_browser=True)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                httpd.shutdown()
            print("  dashboard stopped.")
            continue
        if raw.startswith("tool "):
            try:
                argv = shlex.split(raw[5:])
                name, args = argv[0], {}
                for pair in argv[1:]:
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        args[k] = v
                result = run_tool(store, name, args)
                print("  " + _str(result).replace("\n", "\n  "))
            except (ToolError, IndexError) as exc:
                print(_c(f"  ! {exc}", "red"))
            continue

        # fast paths
        if raw.lower().startswith("task "):
            parts = raw[5:].rsplit(" on ", 1) if " on " in raw.lower() else raw[5:].rsplit(" ", 1), None
            title, due = (parts if len(parts) == 2 else (raw[5:], None))
            res = run_tool(store, "add_task", {"title": title.strip(), "due": due})
            print("  " + _str(res)); continue
        if raw.lower().startswith("note "):
            res = run_tool(store, "remember", {"text": raw[5:]})
            print("  " + _str(res)); continue
        if raw.lower().startswith("sched "):
            body = raw[6:]
            when = body.rsplit(" ", 2)[-2:]
            res = run_tool(store, "add_event", {"title": body.rsplit(" ", 2)[0].strip(), "start": " ".join(when)})
            print("  " + _str(res)); continue
        if raw.lower() in {"today", "overdue", "conflicts", "free", "brief", "tasks", "drafts"}:
            scope = {"today": "list_tasks", "overdue": "list_tasks", "drafts": "list_drafts"}.get(raw.lower(), raw.lower())
            res = run_tool(store, scope, {"scope": "overdue"} if raw.lower() == "overdue" else {})
            print("  " + _str(res)); continue

        reply = engine.respond(raw, history)
        if reply.degraded and reply.note:
            print(_c(f"  ({reply.note})", "yellow"))
        print("  " + _c(reply.text.replace("\n", "\n  "), "green"))
        for action in reply.actions:
            print(_c(f"  · {action.get('tool')}", "grey"))
        history.append({"role": "user", "content": raw})
        history.append({"role": "assistant", "content": reply.text})


def _str(result: dict) -> str:
    from .timeutil import from_iso

    if "message" in result and not any(k in result for k in ("tasks", "events", "memory", "drafts", "conflicts")):
        return result["message"]
    lines = []
    for key in ("tasks", "events", "memory", "drafts", "conflicts", "slots"):
        for item in result.get(key, []) or []:
            if "title" in item:
                extra = item.get("label") or item.get("due_human") or item.get("day_label") or ""
                lines.append(f"- {item['title']}" + (f"  ({extra})" if extra else ""))
            elif "text" in item:
                lines.append(f"- {item['text']}")
            elif "start" in item:
                lines.append(f"- {fmt_range(from_iso(item['start']), from_iso(item['end']))}")
            elif "a" in item:
                lines.append(f"- clash: {item['a']['title']} vs {item['b']['title']}")
    return "\n".join(lines) or result.get("message", "")


def main(store: Store) -> int:
    return run(store)
