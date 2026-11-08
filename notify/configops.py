"""Validate, persist and apply a configuration change in one step."""

from __future__ import annotations

import threading
from typing import Any, Callable

from .config import ConfigError, NotifyConfig
from .store import edit_config


def commit_config(app: Any, mutate: Callable[[dict[str, Any]], None]) -> NotifyConfig:
    """Raises ConfigError (nothing saved) when the result does not validate. Serialised: the admin routes run on
    the event loop and browser enrollment in a worker thread, and each is a read-modify-write of the whole config."""
    with _lock:
        new = edit_config(app.state.config, mutate)
        _refuse_new_shared_mailboxes(app.state.config, new)
        app.state.store.save(new)
        app.state.apply_config(new)
        return new


_lock = threading.RLock()


def reply_mailbox_users(cfg: NotifyConfig) -> dict[tuple[str, int, str, str], list[str]]:
    """Providers per reply mailbox folder (IMAP host, port, user, folder to read)."""
    out: dict[tuple[str, int, str, str], list[str]] = {}
    for name, p in cfg.providers.items():
        s = p.model_dump()
        host = str(s.get("imap_host") or "").strip().lower()
        if p.type != "email" or not host:
            continue
        folder = str(s.get("reply_inbox") or "INBOX")
        folder = "INBOX" if folder.upper() == "INBOX" else folder  # INBOX is case-insensitive in IMAP
        try:
            port = int(s.get("imap_port") or 993)
        except (TypeError, ValueError):
            port = 993
        key = (host, port, str(s.get("imap_user") or "").strip().lower(), folder)
        out.setdefault(key, []).append(name)
    return out


def _refuse_new_shared_mailboxes(old: NotifyConfig, new: NotifyConfig) -> None:
    """Two providers reading one folder quarantine each other's replies (no_thread). A change may not create such a
    pair; one that already existed does not block unrelated changes."""
    before = {k for k, v in reply_mailbox_users(old).items() if len(v) > 1}
    for key, names in reply_mailbox_users(new).items():
        if len(names) > 1 and key not in before:
            raise ConfigError(f"providers {', '.join(sorted(names))} would read the same reply mailbox folder"
                              f" ({key[3]} of {key[2]}@{key[0]}); give each provider its own mailbox or folder")
