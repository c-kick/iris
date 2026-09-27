"""Validate, persist and apply a configuration change in one step."""

from __future__ import annotations

from typing import Any, Callable

from .config import NotifyConfig
from .store import edit_config


def commit_config(app: Any, mutate: Callable[[dict[str, Any]], None]) -> NotifyConfig:
    """Raises ConfigError (nothing saved) when the result does not validate."""
    new = edit_config(app.state.config, mutate)
    app.state.store.save(new)
    app.state.apply_config(new)
    return new
