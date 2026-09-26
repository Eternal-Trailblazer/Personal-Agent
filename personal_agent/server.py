"""Local REST API + static server. Standard library only, binds to 127.0.0.1.

Routes are declared in ``ROUTES`` as (method, pattern, handler). The handler
receives (store, match, body) and returns a JSON-serialisable dict.
"""

from __future__ import annotations

import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .brain import get_engine
from .config import (
    APP_NAME,
    PROVIDER_PRESETS,
    VERSION,
    WEB_DIR,
    Settings,
    load_llm_settings,
)
from .prompt import build_system_prompt
from .store import Store
from .timeutil import TZ
from .tools import ToolError, run_tool

Handler = Callable[[Store, re.Match, dict], Any]
ROUTES: list[tuple[str, str, Handler]] = []


def route(method: str, pattern: str):
    def wrap(fn: Handler) -> Handler:
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn))
        return fn

    return wrap


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@route("GET", r"/api/health")
def health(store, match, body):
    from .config import load_llm_settings
    engine = get_engine(store)
    llm = load_llm_settings(store)
    return {"ok": True, "app": APP_NAME, "version": VERSION,
            "engine": engine.name, "llm_configured": llm.enabled,
            "llm_model": llm.model if llm.enabled else None,
            "provider": store.get_setting("provider", "auto"), "tz": TZ.name}


@route("GET", r"/api/overview")
def overview(store, match, body):
    return store.overview()


@route("GET", r"/api/tasks")
def list_tasks(store, match, body):
    scope = (body.get("scope") or "open").lower()
    status = "all" if scope == "all" else scope if scope in {"done", "dropped"} else None
    return {"tasks": store.list_tasks(status=status, project=body.get("project") or None)}


@route("POST", r"/api/tasks")
def create_task(store, match, body):
    return {"task": store.add_task(
        body.get("title", ""), notes=body.get("notes", ""),
        due_at=body.get("due_at") or None, priority=body.get("priority", 2),
        project=body.get("project", ""), status=body.get("status", "open"))}


@route("PATCH", r"/api/tasks/(\d+)")
def patch_task(store, match, body):
    return {"task": store.update_task(int(match.group(1)), **{k: v for k, v in body.items() if v is not None})}


@route("DELETE", r"/api/tasks/(\d+)")
def delete_task(store, match, body):
    return {"deleted": store.delete_task(int(match.group(1)))}


@route("GET", r"/api/events")
def list_events(store, match, body):
    from .timeutil import parse_when
    target = parse_when(body.get("day") or "today") or TZ.now()
    events = store.list_events(start=body.get("start"), end=body.get("end")) if body.get("start") else store.agenda(target.date())
    return {"events": events, "free_slots": store.free_slots(target.date()),
            "conflicts": store.conflicts(*_bounds(target))}


def _bounds(dt):
    from .timeutil import day_bounds
    lo, hi = day_bounds(dt.date())
    return lo.strftime("%Y-%m-%dT%H:%M"), hi.strftime("%Y-%m-%dT%H:%M")


@route("POST", r"/api/events")
def create_event(store, match, body):
    # Accept natural language for start/end; default a 60-minute block.
    from datetime import timedelta

    from .timeutil import parse_when
    start = parse_when(body.get("start_at") or "now")
    if not start:
        raise ValueError(f"could not understand start_at: {body.get('start_at')!r}")
    end = parse_when(body.get("end_at"), base=start) if body.get("end_at") else start + timedelta(hours=1)
    if not end or end <= start:
        end = start + timedelta(hours=1)
    event = store.add_event(
        body["title"], start.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M"),
        kind=body.get("kind", "class"), location=body.get("location", ""), notes=body.get("notes", ""))
    result = {"event": event}
    lo, hi = _bounds(start)
    clashes = [c for c in store.conflicts(lo, hi)
               if event["id"] in (c["a"]["id"], c["b"]["id"])]
    if clashes:
        other = clashes[0]["a"] if clashes[0]["b"]["id"] == event["id"] else clashes[0]["b"]
        result["warning"] = f"Overlaps {other['title']} by {clashes[0]['overlap_min']} min"
    return result


@route("PATCH", r"/api/events/(\d+)")
def patch_event(store, match, body):
    return {"event": store.update_event(int(match.group(1)), **{k: v for k, v in body.items() if v is not None})}


@route("DELETE", r"/api/events/(\d+)")
def delete_event(store, match, body):
    return {"deleted": store.delete_event(int(match.group(1)))}


@route("GET", r"/api/memory")
def list_memory(store, match, body):
    return {"memory": store.recall(body.get("q") or None)}


@route("POST", r"/api/memory")
def add_memory(store, match, body):
    return {"memory": store.remember(body["text"], kind=body.get("kind", "fact"), pinned=body.get("pinned", False))}


@route("DELETE", r"/api/memory/(\d+)")
def delete_memory(store, match, body):
    return {"deleted": store.forget(int(match.group(1)))}


@route("GET", r"/api/drafts")
def list_drafts(store, match, body):
    return {"drafts": store.list_drafts(status=body.get("status", "draft"))}


@route("POST", r"/api/drafts")
def create_draft(store, match, body):
    return {"draft": store.add_draft(
        channel=body.get("channel", "email"), audience=body.get("audience", ""),
        subject=body.get("subject", ""), body=body.get("body", ""))}


@route("PATCH", r"/api/drafts/(\d+)")
def patch_draft(store, match, body):
    return {"draft": store.update_draft(int(match.group(1)), **{k: v for k, v in body.items() if v is not None})}


@route("DELETE", r"/api/drafts/(\d+)")
def delete_draft(store, match, body):
    return {"deleted": store.delete_draft(int(match.group(1)))}


@route("GET", r"/api/settings")
def get_settings(store, match, body):
    # Read the LLM config directly rather than off the active engine — the
    # local engine has no `settings` attribute, and the user still needs to see
    # (and fill in) the base URL while running locally.
    return {"settings": Settings().public(), "provider": store.get_setting("provider", "hybrid"),
            "engine": get_engine(store).name, "llm": load_llm_settings(store).public(),
            "llm_preset": store.get_setting("llm_preset", ""),
            "presets": [{"id": k, **{kk: vv for kk, vv in v.items() if kk != "models"}}
                        for k, v in PROVIDER_PRESETS.items()],
            "preset_models": {k: v["models"] for k, v in PROVIDER_PRESETS.items()},
            "db": str(store.path)}


@route("POST", r"/api/settings")
def set_settings(store, match, body):
    for key in ("provider", "owner", "custom_prompt"):
        if key in body:
            store.set_setting(key, body[key])
    if body.get("tz"):
        from .timeutil import _safe_zone
        try:
            _safe_zone(body["tz"])
        except Exception:
            raise ValueError(f"unknown timezone: {body['tz']}")
        store.set_setting("tz", body["tz"])
        TZ.use(body["tz"])

    # Choosing a preset fills the endpoint and model, so the only thing left
    # for the user to do is paste a key.
    if body.get("llm_preset") is not None:
        from .config import resolve_preset
        name = (body.get("llm_preset") or "").strip().lower()
        store.set_setting("llm_preset", name)
        preset = resolve_preset(name)
        if preset:
            current = store.get_setting("llm", {})
            current["base_url"] = body.get("base_url") or preset["base_url"]
            current["model"] = body.get("model") or preset["model"]
            for limit in ("max_tokens", "timeout"):
                if preset.get(limit):
                    current[limit] = preset[limit]
            if not preset["key_required"]:
                current.setdefault("api_key", "not-required")
            store.set_setting("llm", current)

    if any(k in body for k in ("base_url", "api_key", "model")):
        current = store.get_setting("llm", {})
        for key in ("base_url", "api_key", "model"):
            if body.get(key):
                current[key] = body[key]
        store.set_setting("llm", current)
    return {"ok": True, "provider": store.get_setting("provider", "hybrid"),
            "engine": get_engine(store).name, "tz": TZ.name,
            "llm_preset": store.get_setting("llm_preset", ""),
            "llm": load_llm_settings(store).public()}


@route("POST", r"/api/test_llm")
def test_llm(store, match, body):
    """Live check of the configured provider, including tool-calling."""
    from .brain import diagnose
    return diagnose(store)


@route("DELETE", r"/api/llm_key")
def clear_llm_key(store, match, body):
    current = store.get_setting("llm", {}) or {}
    current.pop("api_key", None)
    store.set_setting("llm", current)
    return {"ok": True, "llm": load_llm_settings(store).public()}


@route("GET", r"/api/tools")
def list_tools(store, match, body):
    from .tools import REGISTRY
    return {"tools": [{"name": t.name, "description": t.description, "group": t.group}
                      for t in REGISTRY]}


@route("POST", r"/api/tools/(\w+)")
def call_tool(store, match, body):
    try:
        return {"ok": True, "result": run_tool(store, match.group(1), body.get("args") or body)}
    except ToolError as exc:
        return {"ok": False, "error": str(exc)}


@route("POST", r"/api/chat")
def chat(store, match, body):
    message = (body.get("message") or "").strip()
    if not message:
        return {"reply": "Say something.", "engine": "local", "actions": []}
    history = body.get("history") or []
    engine = get_engine(store, body.get("provider"))
    reply = engine.respond(message, history)

    # Models are not reliable at volunteering conflict warnings, and a
    # double-booking is exactly the thing the principal should never have to
    # discover by accident. Check it here instead of trusting the model.
    clashes = _new_conflicts(store, reply.actions)
    if clashes:
        listing = "\n".join(f"  • {c}" for c in clashes)
        reply.text = f"{reply.text}\n\nHeads up — that overlaps:\n{listing}"

    return {"reply": reply.text, "engine": reply.engine, "actions": reply.actions,
            "degraded": reply.degraded, "note": reply.note,
            "deferred": reply.deferred, "conflicts": clashes,
            "routing_reason": getattr(engine, "last_reason", "")}


def _new_conflicts(store, actions) -> list[str]:
    """One-line conflicts between newly created events and pre-existing ones.

    Events created in the same turn are excluded from each other's reports —
    booking three study sessions in one message is not a self-conflict.
    """
    from .timeutil import day_bounds, from_iso

    new_ids = set()
    for action in actions or []:
        if action.get("tool") != "add_event":
            continue
        event = (action.get("result") or {}).get("event") or {}
        if event.get("id"):
            new_ids.add(event["id"])

    found: list[str] = []
    for action in actions or []:
        if action.get("tool") != "add_event":
            continue
        event = (action.get("result") or {}).get("event") or {}
        event_id = event.get("id")
        title = event.get("title") or (action.get("args") or {}).get("title") or "New event"
        # Prefer the resolved timestamps from the created row; the model's own
        # arguments ("8:00 AM") carry no date and cannot be reasoned about.
        start_dt, end_dt = from_iso(event.get("start_at")), from_iso(event.get("end_at"))
        if not start_dt or not end_dt:
            args = action.get("args") or {}
            start_dt = from_iso(args.get("start") or args.get("start_at"))
            end_dt = from_iso(args.get("end") or args.get("end_at"))
        if not start_dt or not end_dt:
            continue
        lo, hi = day_bounds(start_dt.date())
        for clash in store.conflicts(lo.strftime("%Y-%m-%dT%H:%M"),
                                     hi.strftime("%Y-%m-%dT%H:%M")):
            for other in (clash["a"], clash["b"]):
                if other["id"] == event_id or other["id"] in new_ids:
                    continue
                found.append(f"{title} vs {other['title']} "
                             f"({clash['overlap_min']} min, {other['time_label']})")
                break
    return found


@route("GET", r"/api/search")
def search(store, match, body):
    return store.global_search(body.get("q") or "")


# --------------------------------------------------------------------------- #
# static files
# --------------------------------------------------------------------------- #
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".ico": "image/x-icon",
        ".json": "application/json; charset=utf-8", ".woff2": "font/woff2"}


class Handler(BaseHTTPRequestHandler):
    store: Store
    server_version = f"PersonalAgent/{VERSION}"

    def log_message(self, fmt, *args):  # quieter console
        pass

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")

    def do_DELETE(self):
        self._route("DELETE")

    # -- internals ------------------------------------------------------- #
    def _route(self, method: str):
        parsed = urlparse(self.path)
        path, query = parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}
        body = dict(query)
        if method in {"POST", "PATCH", "DELETE"}:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body.update(json.loads(raw.decode()))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return self._json({"error": "invalid JSON body"}, 400)

        for verb, pattern, fn in ROUTES:
            if verb != method:
                continue
            m = pattern.match(path)
            if m:
                try:
                    return self._json(fn(self.store, m, body))
                except ToolError as exc:
                    return self._json({"error": str(exc)}, 400)
                except KeyError as exc:
                    return self._json({"error": f"missing field: {exc}"}, 400)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                except Exception as exc:  # pragma: no cover
                    return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        if method == "GET":
            return self._static(path)
        self._json({"error": "not found"}, 404)

    def _json(self, payload: Any, status: int = 200):
        data = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            target = WEB_DIR / "index.html"  # SPA fallback
        if not target.is_file():
            return self._json({"error": "not found"}, 404)
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _free_port(preferred: int) -> int:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(store: Store, port: int = 8765, open_browser: bool = True) -> ThreadingHTTPServer:
    Handler.store = store
    actual = _free_port(port)
    httpd = ThreadingHTTPServer(("127.0.0.1", actual), Handler)
    url = f"http://127.0.0.1:{actual}"
    llm = load_llm_settings(store)
    print(f"  {APP_NAME} v{VERSION}")
    print(f"  Dashboard  {url}")
    print(f"  Engine     {get_engine(store).name}")
    if llm.enabled:
        print(f"  Cloud LLM  {llm.model} via {llm.base_url}")
    print(f"  Database   {store.path}")
    print("  Ctrl+C to stop\n")
    if open_browser:
        threading.Timer(0.6, lambda: __import__("webbrowser").open(url)).start()
    return httpd
