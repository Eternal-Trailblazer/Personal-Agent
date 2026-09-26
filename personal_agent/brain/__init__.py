"""Pluggable intelligence layer. See ``engine.py`` for the contract."""

from .engine import (
    Engine,
    HybridEngine,
    LocalEngine,
    OpenAIEngine,
    Reply,
    diagnose,
    get_engine,
)

__all__ = [
    "Engine", "HybridEngine", "LocalEngine", "OpenAIEngine",
    "Reply", "diagnose", "get_engine",
]
