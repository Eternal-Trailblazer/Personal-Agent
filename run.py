#!/usr/bin/env python3
"""Personal Agent — entry point.

    python run.py                 launch the dashboard (default)
    python run.py chat            terminal mode
    python run.py serve --port 9000
    python run.py seed            load a sample week
    python run.py tool add_task title="Submit report" due="friday 5pm"

    python run.py key --provider gemini      point at a free provider
    python run.py test                       verify key + tool-calling
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows consoles default to cp1252 and blow up on the em-dashes and arrows
# used in output. Force UTF-8 so the CLI renders cleanly everywhere.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

from personal_agent.brain import get_engine          # noqa: E402
from personal_agent.config import APP_NAME, VERSION  # noqa: E402
from personal_agent.demo import seed                 # noqa: E402
from personal_agent.server import serve              # noqa: E402
from personal_agent.store import Store               # noqa: E402
from personal_agent.timeutil import TZ               # noqa: E402
from personal_agent.tools import ToolError, run_tool  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(prog="personal-agent", description=f"{APP_NAME} v{VERSION}")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="launch the web dashboard")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--no-open", action="store_true")

    sub.add_parser("chat", help="terminal mode")
    sub.add_parser("seed", help="load demo data")
    sub.add_parser("doctor", help="print environment + engine diagnostics")

    p_tool = sub.add_parser("tool", help="invoke a tool directly")
    p_tool.add_argument("name")
    p_tool.add_argument("args", nargs="*")

    p_ask = sub.add_parser("ask", help="one-shot question to the active engine")
    p_ask.add_argument("message", nargs="+")

    p_key = sub.add_parser("key", help="configure the model provider")
    p_key.add_argument("--provider", default=None,
                       help="gemini | groq | openrouter | ollama | custom")
    p_key.add_argument("--set", default=None, metavar="API_KEY",
                       help="store an API key (prefer the Settings page)")
    p_key.add_argument("--model", default=None, help="model id")
    p_key.add_argument("--base-url", default=None, help="OpenAI-compatible base URL")
    p_key.add_argument("--clear", action="store_true", help="remove the stored key")
    p_key.add_argument("--mode", default=None, choices=["hybrid", "local", "openai"],
                       help="how much work to send to the cloud")

    sub.add_parser("test", help="check provider connectivity and tool-calling")

    args = parser.parse_args()
    store = Store()
    store.set_setting("tz", store.get_setting("tz", TZ.name))
    TZ.use(store.get_setting("tz", TZ.name))

    if args.cmd == "serve":
        serve(store, port=args.port, open_browser=not args.no_open).serve_forever()
        return 0

    if args.cmd == "chat":
        from personal_agent.cli import main as cli_main
        return cli_main(store)

    if args.cmd == "seed":
        result = seed(store)
        print(json.dumps(result, indent=2))
        return 0

    if args.cmd == "doctor":
        from personal_agent.config import load_llm_settings
        engine = get_engine(store)
        llm = load_llm_settings(store)
        print(f"{APP_NAME} {VERSION}")
        print(f"  python    {sys.version.split()[0]}")
        print(f"  database  {store.path}")
        print(f"  timezone  {TZ.name} ({TZ.offset_label()})")
        print(f"  now       {TZ.now():%Y-%m-%d %H:%M}")
        print(f"  engine    {engine.name}")
        print(f"  provider  {store.get_setting('provider', 'auto')}")
        print(f"  cloud llm {'configured → ' + llm.model if llm.enabled else 'not configured (fully local)'}")
        print(f"  tasks     {store.task_counts()}")
        return 0

    if args.cmd == "tool":
        try:
            tool_args = {}
            for pair in args.args:
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    tool_args[k] = v
            print(json.dumps(run_tool(store, args.name, tool_args), indent=2, default=str))
            return 0
        except ToolError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.cmd == "key":
        from personal_agent.config import PROVIDER_PRESETS, resolve_preset
        if args.clear:
            cfg = store.get_setting("llm", {}) or {}
            cfg.pop("api_key", None)
            store.set_setting("llm", cfg)
            print("  API key removed.")
        if args.provider:
            store.set_setting("llm_preset", args.provider)
            preset = resolve_preset(args.provider)
            if preset:
                cfg = store.get_setting("llm", {}) or {}
                cfg["base_url"] = args.base_url or preset["base_url"]
                cfg["model"] = args.model or preset["model"]
                for limit in ("max_tokens", "timeout"):
                    if preset.get(limit):
                        cfg[limit] = preset[limit]
                if not preset["key_required"]:
                    cfg.setdefault("api_key", "not-required")
                store.set_setting("llm", cfg)
                print(f"  provider  {preset['label']}")
                print(f"  base_url  {cfg['base_url']}")
                print(f"  model     {cfg['model']}")
            else:
                print(f"  unknown provider '{args.provider}'. "
                      f"Choose from: {', '.join(PROVIDER_PRESETS)}", file=sys.stderr)
                return 1
        if args.set:
            cfg = store.get_setting("llm", {}) or {}
            cfg["api_key"] = args.set
            store.set_setting("llm", cfg)
            print("  API key stored in agent.db (local only).")
        if args.base_url or args.model:
            cfg = store.get_setting("llm", {}) or {}
            if args.base_url:
                cfg["base_url"] = args.base_url
            if args.model:
                cfg["model"] = args.model
            store.set_setting("llm", cfg)
        if args.mode:
            store.set_setting("provider", args.mode)
            print(f"  mode      {args.mode}")
        print(f"  engine    {get_engine(store).name}")
        return 0

    if args.cmd == "test":
        from personal_agent.brain import diagnose
        r = diagnose(store)
        print(f"  endpoint  {r['base_url']}")
        print(f"  model     {r['model']}")
        print(f"  key       {'set' if r['key_set'] else 'not set'}")
        if r.get("error"):
            print(f"  ✕ {r['error']}")
            return 1
        print(f"  ✓ reachable ({r['latency_ms']} ms)")
        print(f"  ✓ key accepted · replied: {r.get('sample') or '—'}")
        if r.get("tools_ok"):
            print(f"  ✓ tool calling works ({r.get('tool_probe')})")
        elif r.get("tool_error"):
            print(f"  ! tool calling untested — {r['tool_error']}")
        else:
            print("  ✕ tool calling not available — the model can chat but cannot act.")
        if r.get("warning"):
            print(f"  ! {r['warning']}")
        return 0 if r.get("tools_ok") or r.get("tool_error") else 1

    if args.cmd == "ask":
        engine = get_engine(store)
        reply = engine.respond(" ".join(args.message))
        print(reply.text)
        return 0

    serve(store, port=8765).serve_forever()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
