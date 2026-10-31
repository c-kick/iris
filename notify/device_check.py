"""Device checks: what a provider last said about a device's address, and one alert when a device breaks.

A device that turns invalid gets a flag in `meta` and one alert to the announce targets; the flag goes when
a later check passes, so a device that breaks again alerts again. The flag outlives restarts."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from .cleanup_service import record_event
from .db import now_iso
from .providers.base import AddressCheck
from .registrations import announce_targets

if TYPE_CHECKING:
    from .core import NotifyService

log = logging.getLogger("notify.device_check")


def _flag(device: str) -> str:
    return f"device_invalid:{device}"


def record_check(svc: NotifyService, device: str, check: AddressCheck) -> None:
    if check.valid:
        with svc.db.tx() as c:
            cleared = c.execute("DELETE FROM meta WHERE key=?", (_flag(device),)).rowcount
        if cleared:
            log.info("device valid again", extra={"device": device})
            record_event(svc, "device.valid", "hnl-notify", {"device": device})
        return
    with svc.db.tx() as c:
        new = c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                        (_flag(device), check.problem or now_iso())).rowcount
    if not new:
        return  # already flagged and announced
    log.warning("device invalid", extra={"device": device, "problem": check.problem})
    record_event(svc, "device.invalid", "hnl-notify", {"device": device, "problem": check.problem})
    _alert(svc, device, check.problem or "the provider rejects it")


def _alert(svc: NotifyService, device: str, problem: str) -> None:
    from .core import SYSTEM_CLIENT, Invalid
    from .models import NotificationIn, Severity
    targets = announce_targets(svc.config)
    if not targets:
        log.warning("device invalid, nobody to tell", extra={"device": device})
        return
    req = NotificationIn(
        targets=targets, title=f"Device {device} no longer works", severity=Severity.normal,
        message=(f"{problem}. Pushes to {device} fail until this is fixed: re-add the device at the provider, "
                 "correct its name in hnl-notify, or disable it."),
        tags=["device-check"], source="hnl-notify/device-check")
    base = svc.config.server.external_url or os.environ.get("NOTIFY_EXTERNAL_URL")
    if base:
        req.url, req.url_title = f"{base.rstrip('/')}/admin#devices", "Open the devices"
    req._internal = True
    try:
        svc.submit(SYSTEM_CLIENT, req)
    except Invalid as e:
        log.warning("device alert not sent", extra={"device": device, "error": str(e)})


async def check_devices(svc: NotifyService) -> None:
    """Ask the providers about every enabled device now (start-up, configuration change), one at a time."""
    for name, dev in list(svc.config.devices.items()):
        provider = svc.providers.get(dev.provider)
        if provider is None or not dev.enabled:
            continue
        try:
            check = await provider.check_address(dev.address, force=True)
        except Exception:  # a check never takes the service down
            log.exception("device check crashed", extra={"device": name})
            continue
        if check is not None:
            record_check(svc, name, check)
