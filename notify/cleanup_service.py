"""Running mailbox cleanup as part of the service: status, the cleanup event and the size alert."""

from __future__ import annotations

import logging
import time
from datetime import timezone
from typing import TYPE_CHECKING, Any

from .db import dumps, now_iso, utcnow
from .mailbox_cleanup import MIB, CleanupMailbox, run_pass
from .providers.email_inbound import mask_secret
from .registrations import announce_targets

if TYPE_CHECKING:
    from .core import NotifyService

log = logging.getLogger("notify.cleanup")


async def run_mailbox_cleanup(svc: NotifyService, name: str, *, dry_run: bool, by: str,
                              deadline_seconds: float | None) -> dict[str, Any]:
    """One pass over the provider's Processed and Quarantine folders. `error` in the result: the pass could not run."""
    from .core import NotFound
    p = svc.providers.get(name)
    if p is None or not getattr(p, "replies_ready", False):
        raise NotFound(f"provider {name} has no reply mailbox")
    cfg = svc.config.cleanup_for(name)

    def opener() -> CleanupMailbox:
        return svc.open_cleanup_mailbox(p) if svc.open_cleanup_mailbox else CleanupMailbox.for_provider(p)

    deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds
    try:
        result = await run_pass(opener, svc.mailbox_lock(name), p, cfg, now=utcnow(), dry_run=dry_run, deadline=deadline)
        result["error"] = None
    except Exception as e:
        result = {"refused": None, "folders": {},
                  "error": mask_secret(f"{type(e).__name__}: {e}", getattr(p, "imap_password", "") or "")[:300]}
    if dry_run:
        return result
    failed = result["error"] or next((f["error"] for f in result["folders"].values() if f["error"]), None)
    st = svc.cleanup_status.setdefault(name, {})
    st.update(last_run=now_iso(), last_by=by, last_result=result, last_error=failed)
    if not failed and not result["refused"]:
        st["last_success"] = st["last_run"]
    st.setdefault("last_success", None)
    log.info("mailbox cleanup", extra={"client": by, "provider": name, "dry_run": False})
    removed = {f: {k: v[k] for k in ("removed_age", "removed_cap", "removed_bytes")}
               for f, v in result["folders"].items()}
    with svc.db.tx() as c:
        c.execute("INSERT INTO events(type, notification_id, delivery_id, client_id, source, data_json, created_at)"
                  " VALUES ('mailbox.cleanup', NULL, NULL, ?, 'hnl-notify', ?, ?)",
                  (by, dumps({"provider": name, "dry_run": False, "removed": removed}), now_iso()))
    for folder, f in result["folders"].items():
        if f["removed_cap"] > 0:
            _alert(svc, name, folder, f, cfg.processed_max_mib if folder == p.reply_processed
                   else cfg.quarantine_max_mib, cfg.alert_targets)
    return result


def _alert(svc: NotifyService, provider: str, folder: str, f: dict[str, Any], cap_mib: int, targets: list[str]) -> None:
    from .core import SYSTEM_CLIENT, Invalid
    from .models import NotificationIn, Severity
    targets = targets or announce_targets(svc.config)
    if not targets:
        log.warning("mailbox over its size cap, nobody to tell", extra={"provider": provider, "folder": folder})
        return
    day = utcnow().astimezone(timezone.utc).strftime("%Y-%m-%d")
    req = NotificationIn(
        targets=targets, title=f"Reply mailbox: {folder} over its size cap", severity=Severity.normal,
        message=(f"{f['removed_cap']} mails ({f['removed_cap_bytes'] / MIB:.1f} MiB) were removed from {folder} because "
                 f"it went over its {cap_mib} MiB cap. It now holds {f['after_bytes'] / MIB:.1f} MiB."),
        tags=["mailbox-cleanup"], idempotency_key=f"mailbox-cleanup:{provider}:{folder}:{day}",
        source="hnl-notify/mailbox-cleanup")
    req._internal = True
    try:
        svc.submit(SYSTEM_CLIENT, req)
    except Invalid as e:
        log.warning("mailbox size alert not sent", extra={"provider": provider, "error": str(e)})
