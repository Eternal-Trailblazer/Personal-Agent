"""Personal Agent — a local-first personal assistant.

Runs entirely on your machine with the standard library only. The intelligence
layer is pluggable: a deterministic local engine ships by default, and an
OpenAI-compatible engine can be switched on later without touching code.
"""

from .config import APP_NAME, VERSION, Settings
from .store import Store
from .tools import REGISTRY, run_tool
from .brain import Engine, LocalEngine, OpenAIEngine, Reply, get_engine

__all__ = [
    "APP_NAME", "VERSION", "Settings",
    "Store", "REGISTRY", "run_tool",
    "Engine", "LocalEngine", "OpenAIEngine", "Reply", "get_engine",
]
__version__ = VERSION
