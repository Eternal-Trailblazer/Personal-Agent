# Personal Agent

A local-first personal assistant for Shekhar Rana. Runs entirely on your machine
with **zero dependencies** — Python's standard library only. The intelligence
layer is pluggable, so it works fully offline today and can be pointed at an
OpenAI-compatible API later without touching any code.

```
┌──────────────────────────────────────────────────────────────┐
│  Interface     Dashboard (web)   ·   Terminal   ·   REST     │
├──────────────────────────────────────────────────────────────┤
│  Brain         LocalEngine  ─┐                               │
│                            ├─ brain/engine.py  (one iface)  │
│                OpenAIBrain  ─┘   ← flip on later             │
├──────────────────────────────────────────────────────────────┤
│  Core          store · tools · timeutil · prompt · memory    │
├──────────────────────────────────────────────────────────────┤
│  Data          data/agent.db  (SQLite, yours, on disk)       │
└──────────────────────────────────────────────────────────────┘
```

## Quick start

```bash
cd personal-agent
python run.py            # launches the dashboard, opens your browser
python run.py seed       # optional: load a sample week
```

No install step, no `pip install`, no build. Python 3.10+ only. It works fully
offline from this point — connecting a model is optional and covered below.

Other entry points:

```bash
python run.py chat                          # terminal mode
python run.py serve --port 9000             # dashboard, fixed port, no browser
python run.py doctor                        # environment + engine diagnostics
python run.py ask "what's due today"        # one-shot question
python run.py tool add_task title="Report" due="friday 5pm"
python tests/test_agent.py                  # test suite
```

## Connect a free model (optional, ~2 minutes)

### In the dashboard

**Settings → Intelligence layer**:

1. Provider → **Smart hybrid** (default)
2. Free provider → **Google Gemini** — the base URL and model fill in automatically
3. Paste your key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) into **API key**
4. **Save**, then **Test connection**

Your key goes into `data/agent.db` on your machine and is sent only to the
endpoint shown. It is never written into the chat.

### From the terminal

```bash
python run.py key --provider gemini        # set endpoint + model
python run.py test                         # verify key and tool-calling
python run.py key --set "YOUR_KEY"         # or store it here
python run.py key --clear                  # remove it again
```

`test` reports each layer separately:

```
  ✓ reachable (412 ms)
  ✓ key accepted · replied: OK
  ✓ tool calling works (add_task)
```

Tool-calling is checked on purpose. A model can answer beautifully and still be
unable to *act* — if that line fails, the agent would chat but never create a
task. Switch to `gemini-2.5-flash-lite` if it happens.

### Providers

| Preset | Key needed | Notes |
|---|---|---|
| **Google Gemini** | yes | Default `gemini-3.1-flash-lite` — generous free quota. |
| Groq | yes | Fastest responses, reliable tool-calling |
| OpenRouter | yes | Many `:free` models; quality varies by model |
| Ollama | **no** | Runs on your machine, offline, unlimited. Weaker tool-calling. |
| Custom | yes | Any `/chat/completions` endpoint |

All of them speak the same OpenAI-compatible protocol, so one code path covers
them all. Ollama and LM Studio need no key at all — a placeholder activates them.

### A note on Gemini model choice

Gemini's free tier retires model ids and meters them aggressively. Measured
against a real key:

| Model | Free tier | Tool calling |
|---|---|---|
| `gemini-3.1-flash-lite` | generous | works — **default** |
| `gemini-3.8-flash` | ~20 requests/day | works |
| `gemini-2.5-flash`, `gemini-2.5-flash-lite` | retired | 404 for new keys |

So the default is the lite model, and retired ids are recorded in the preset's
`retired` list — they are never offered in the UI and never used as fallbacks.
If a model is rate-limited or overloaded, the engine retries with backoff, rolls
over to the next model in the chain, remembers which one worked, and tells you
it switched. It only falls back to the local engine when all of that fails.

## How routing works (smart hybrid)

```
your message
     │
     ├─ clearly needs reasoning? ────────► cloud  ("is a question")
     │                                      ("asks for reasoning")
     │                                      ("multi-part request")
     ▼
  local engine  ── handled it ──────────► done   ⚡ instant, 0 tokens
     │
     └── deferred (didn't recognise) ────► cloud
                                            │
                                            └── unavailable / failed ──► local
```

This is why simple requests stay fast and free: `add task buy lab goggles friday 5pm`
and `what's due today` never touch the network. Only ambiguous or genuinely
reasoning-heavy messages cost tokens. Every reply shows which path it took
(`handled locally`, `is a question`, `spans several dates`, …) in the chat
footer.

Requests that need real date arithmetic are always escalated: the system prompt
carries an explicit fortnight map (`Sunday 27 Sep (2026-09-27)`), because
models cannot reliably convert weekday names to dates in their head. Booking
"Sunday, Monday and Tuesday" therefore produces three correctly-dated events,
and a repeat of the same booking in the same message is collapsed to one.

Conflicts are detected by the app, not the model — booking something that
overlaps your timetable always tells you, even if the model forgets to mention
it.

Three modes are available: **Smart hybrid** (default), **Local only** (never
sends anything), **Cloud for everything**.

## What it does

| Area | Capability |
|---|---|
| **Schedule** | Block time, natural-language times (`friday 5pm`, `tomorrow 9am`, `26 sep 14:00`), overlap detection, free-slot finder, day agenda |
| **Tasks** | Capture, priority (P1–P3), projects, due dates, overdue tracking, open/today/overdue/done filters |
| **Communication** | Email and message drafts saved for review — never auto-sent |
| **Memory** | Durable facts, preferences and commitments, injected into every system prompt so preferences persist across sessions |
| **Synthesis** | `brief` — one line combining counts, focus task, agenda, conflicts and free time |

## The hybrid design

The point of the architecture is that **adding cloud reasoning is a settings
change, not a rewrite.**

- **Local** — a deterministic intent parser. No network, no cost, no API key.
  Handles capture, scheduling, review and drafting, and explicitly declines
  anything ambiguous rather than guessing.
- **Cloud** — tool-calling against any OpenAI-compatible `/chat/completions`
  endpoint. Receives the same system prompt and the same 15 tools, so it can
  only do what the local engine could.
- **Hybrid** — the router between them (diagram above).
- Either way it degrades: any network, auth or rate-limit error falls back to
  the local engine and flags the reply.

### Adding a third engine

Implement the two-method `Engine` protocol in `brain/engine.py`:

```python
class MyEngine:
    name = "my-engine"
    def available(self) -> bool: ...
    def respond(self, message: str, history: list[dict]) -> Reply: ...
```

Return a `Reply(text=..., actions=[...])`. Register it in `get_engine()`. Nothing
else in the codebase needs to change.

Keys are stored in `data/agent.db` and sent nowhere except the endpoint you set.
`delete_task` is never exposed to any model — deletes are an explicit UI action
behind a confirmation.

## Tools

Fifteen tools, all callable from the UI, the CLI, the REST API, or the LLM:

```
add_task        list_tasks       complete_task    update_task      delete_task
add_event       list_events      find_conflicts   free_slots
remember        recall           forget
create_draft    list_drafts
daily_brief
```

```bash
curl -X POST localhost:8765/api/tools/add_event \
  -H 'Content-Type: application/json' \
  -d '{"args":{"title":"Lecture","start":"tomorrow 4pm","end":"tomorrow 6pm"}}'
```

## The system prompt

`prompt.py` holds the operating brief — identity, mandate, decisiveness over
deference, signal over noise, reversibility checks, output format, and the
high-stakes boundaries. The working-memory block is injected live from the
database on every request, so a preference stated once applies from then on.

Add personal instructions in Settings → *Extra instructions*.

## Project layout

```
personal-agent/
├── run.py                    entry point
├── personal_agent/
│   ├── config.py             env-driven settings, LLM config
│   ├── store.py              SQLite DAL (tasks, events, memory, drafts)
│   ├── tools.py              the 15 tools + JSON schemas
│   ├── prompt.py             system prompt + live working memory
│   ├── timeutil.py           natural-language time parsing
│   ├── server.py             REST API + static server (stdlib)
│   ├── cli.py                terminal interface
│   ├── demo.py               sample data
│   ├── brain/
│   │   └── engine.py         LocalEngine · OpenAIEngine · get_engine
│   └── web/                  index.html · styles.css · app.js
├── tests/                    test_agent.py · test_timeutil.py
└── data/agent.db             created on first run
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PA_TZ` | `Asia/Kolkata` | IANA timezone |
| `PA_OWNER` | `Shekhar` | Display name in the prompt |
| `PA_DATA_DIR` | `./data` | Database location |
| `PA_LLM_API_KEY` | — | Model key; enables the cloud engine |
| `PA_LLM_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible URL |
| `PA_LLM_MODEL` | `gpt-4o-mini` | Model id |
| `PA_LLM_TIMEOUT` | `45` | Request timeout, seconds |

Env vars take priority over the database; anything set in Settings is stored
locally.

## Design notes

- **Timestamps are stored as local wall-clock** (`YYYY-MM-DDTHH:MM`), naive by
  design. Single user, single timezone — this keeps the database
  human-readable and free of offset drift. Every write normalises through
  `_iso_or_die`, so natural language and ISO input converge on one format, and
  an unparseable time raises instead of silently corrupting conflict detection.
- **Binds to `127.0.0.1` only.** Not exposed to your network.
- **Deletes are never automatic.** The LLM has no `delete_task`; the UI asks
  first; the CLI reports what's going.
- **Stdlib only.** `urllib` for HTTP, `sqlite3` for storage, `http.server` for
  serving. Nothing to install, nothing to break on a dependency bump.
