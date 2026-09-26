"""Runtime configuration for Personal Agent.

Everything is overridable by environment variables so the app stays portable
and nothing is hard-coded to a single machine.

    PA_TZ            IANA timezone name           (default Asia/Kolkata / GMT+5:30)
    PA_OWNER         Owner display name            (default Shekhar)
    PA_DATA_DIR      Where agent.db lives          (default ./data)
    PA_LLM_BASE_URL  OpenAI-compatible base URL
    PA_LLM_API_KEY   Enables the LLM brain when set
    PA_LLM_MODEL     Model id
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "Personal Agent"
APP_SLUG = "personal-agent"
VERSION = "1.0.0"
TAGLINE = "Your schedule, tasks and drafts in one place."

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
WEB_DIR = PACKAGE_DIR / "web"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def data_dir() -> Path:
    path = Path(_env("PA_DATA_DIR", str(PROJECT_DIR / "data"))).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "agent.db"


@dataclass(frozen=True)
class LLMSettings:
    """OpenAI-compatible chat-completions configuration."""

    base_url: str = _env("PA_LLM_BASE_URL", "https://api.openai.com/v1")
    api_key: str = _env("PA_LLM_API_KEY", "")
    model: str = _env("PA_LLM_MODEL", "gpt-4o-mini")
    temperature: float = 0.3
    max_tool_rounds: int = 6
    max_tokens: int = int(_env("PA_LLM_MAX_TOKENS", "2048"))
    timeout: int = int(_env("PA_LLM_TIMEOUT", "45"))
    retries: int = int(_env("PA_LLM_RETRIES", "3"))

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def public(self) -> dict:
        """Serialisable snapshot with the secret redacted."""
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_set": self.enabled,
            "key_hint": (self.api_key[:3] + "…" + self.api_key[-2:]) if self.enabled else "",
        }


@dataclass(frozen=True)
class Settings:
    tz: str = _env("PA_TZ", "Asia/Kolkata")
    owner: str = _env("PA_OWNER", "Shekhar")
    workday_start: int = 9      # hour, used for proactive time-blocking suggestions
    workday_end: int = 21
    deep_work_morning: bool = True
    custom_prompt: str = ""

    def public(self) -> dict:
        return {
            "tz": self.tz,
            "owner": self.owner,
            "workday_start": self.workday_start,
            "workday_end": self.workday_end,
            "deep_work_morning": self.deep_work_morning,
            "custom_prompt": self.custom_prompt,
            "app_name": APP_NAME,
            "version": VERSION,
        }


def load_llm_settings(store=None) -> LLMSettings:
    """Environment defaults, overridden by anything saved in the database."""
    base = LLMSettings()
    if store is None:
        return base
    saved = store.get_setting("llm", {}) or {}
    return LLMSettings(
        base_url=saved.get("base_url", base.base_url),
        api_key=saved.get("api_key", base.api_key),
        model=saved.get("model", base.model),
        temperature=float(saved.get("temperature", base.temperature)),
        max_tool_rounds=int(saved.get("max_tool_rounds", base.max_tool_rounds)),
        max_tokens=int(saved.get("max_tokens", base.max_tokens)),
        timeout=int(saved.get("timeout", base.timeout)),
        retries=int(saved.get("retries", base.retries)),
    )


# --------------------------------------------------------------------------- #
# provider presets
# --------------------------------------------------------------------------- #
# Every entry is an OpenAI-compatible /chat/completions endpoint, so a single
# code path covers all of them. `key_required: False` marks endpoints that
# accept any placeholder string — that keeps Ollama/LM Studio working even
# though they have no real key.
PROVIDER_PRESETS: dict[str, dict] = {
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        # Ordered by free-tier quota headroom, not by raw capability. The 3.8/3.5
        # flagships are limited to roughly 20 requests/day on the free plan, which
        # is not enough for an assistant; the lite models have generous limits and
        # were verified to emit tool calls correctly.
        "model": "gemini-3.1-flash-lite",
        "models": [
            ("gemini-3.1-flash-lite", "gemini-3.1-flash-lite — most free quota (Recommended)"),
            ("gemini-3.8-flash", "gemini-3.8-flash — strongest, ~20 req/day on free tier"),
            ("gemini-3.5-flash", "gemini-3.5-flash — newer, limited free quota"),
        ],
        "retired": [
            "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash",
            "gemini-flash-latest", "gemini-flash-lite-latest",
        ],
        "key_hint": "aistudio.google.com/apikey",
        "key_required": True,
        # Thinking models can spend the whole completion budget on reasoning and
        # return an empty string. These are the limits that avoid that.
        "max_tokens": 2048,
        "timeout": 60,
        "note": "Auth is sent as `Authorization: Bearer <key>`, which the OpenAI-compatible "
                "endpoint accepts. Free tier needs no credit card.",
    },
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "models": [
            ("llama-3.3-70b-versatile", "llama-3.3-70b-versatile — best tool calling"),
            ("llama-3.1-8b-instant", "llama-3.1-8b-instant — very fast"),
        ],
        "key_hint": "console.groq.com/keys",
        "key_required": True,
        "max_tokens": 2048,
        "note": "Fastest responses of the free tiers; reliable function calling.",
    },
    "openrouter": {
        "label": "OpenRouter (free models)",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "google/gemini-2.0-flash-exp:free",
        "models": [
            ("google/gemini-2.0-flash-exp:free", "gemini-2.0-flash-exp:free"),
            ("meta-llama/llama-3.3-70b-instruct:free", "llama-3.3-70b-instruct:free"),
        ],
        "key_hint": "openrouter.ai/keys",
        "key_required": True,
        "max_tokens": 2048,
        "note": "Model ids ending in `:free` cost nothing. Tool-calling quality varies by model.",
    },
    "ollama": {
        "label": "Ollama (local, no key)",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b-instruct",
        "models": [
            ("qwen2.5:7b-instruct", "qwen2.5:7b-instruct — best local tool calling"),
            ("llama3.1:8b", "llama3.1:8b"),
            ("mistral-nemo", "mistral-nemo"),
        ],
        "key_hint": "not required",
        "key_required": False,
        "max_tokens": 2048,
        "note": "Runs on your machine, fully offline. Any placeholder key is accepted.",
    },
    "custom": {
        "label": "Custom / OpenAI-compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "models": [],
        "key_hint": "your provider's key",
        "key_required": True,
        "max_tokens": 2048,
        "note": "Any endpoint that speaks /chat/completions works.",
    },
}


def resolve_preset(name: str | None) -> dict | None:
    return PROVIDER_PRESETS.get((name or "").strip().lower())
