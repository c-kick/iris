"""Background loops: dispatcher, receipt poller, webhook sender, sweeper."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any

import httpx

from .core import Client, Conflict, NotifyService
from .db import dumps, now_iso
from .device_check import check_devices, record_check
from . import attachments, netguard
from .providers.base import ProviderError, SendResult
from .providers.email_inbound import mask_secret, poll_once, skipped_note

log = logging.getLogger("notify.worker")


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


_REPLY_SETTINGS = ("imap_host", "imap_port", "imap_user", "imap_password", "authserv_id", "reply_address",
                   "reply_inbox", "reply_processed", "reply_quarantine")


def reconcile_inbound(providers: dict[str, Any], status: dict[str, Any], failures: dict[str, int],
                      next_at: dict[str, float], prints: dict[str, tuple]) -> None:
    """Forget backoff when a provider's reply settings changed; drop state of providers gone or not reply-ready."""
    live = {n: p for n, p in providers.items() if getattr(p, "replies_ready", False)}
    for n, p in live.items():
        fp = tuple(getattr(p, k, None) for k in _REPLY_SETTINGS)
        if n in prints and prints[n] != fp:
            failures.pop(n, None)
            next_at.pop(n, None)
        prints[n] = fp
    for d in (status, failures, next_at, prints):
        for n in [n for n in d if n not in live]:
            del d[n]


async def webhook_address_ok(url: str, resolve: Any) -> bool:
    """Webhooks may go to any LAN host, but never to loopback, link-local or unspecified addresses (the
    container itself, cloud metadata): the same deny list as attachment URLs."""
    try:
        host, _ = attachments._host_port(url)
        ips = [host] if attachments._ip(host) is not None else list(await resolve(host))
    except (attachments.AttachmentError, OSError, ValueError):  # ValueError: e.g. a label longer than 63
        return False
    return bool(ips) and not any(attachments._denied(attachments._ip(x)) for x in ips)


class Workers:
    def __init__(self, service: NotifyService, http: httpx.AsyncClient):
        self.service = service
        self.http = http
        self.heartbeat: dict[str, float] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self._inflight: set[asyncio.Task[Any]] = set()
        self._busy: dict[str, int] = {name: 0 for name in service.providers}
        self._stopping = False
        self.receipt_pause = 5.0  # Pushover: poll no faster than once per 5 s
        self.inbound_interval = 30.0
        self.cleanup_start_delay = 600.0   # first automatic cleanup this long after the provider is first seen
        self.cleanup_interval = 86400.0
        self.cleanup_check_seconds = 60.0
        self.cleanup_backoff_max = 21600.0
        self.clock = time.monotonic        # seam for the cleanup schedule
        self._loop: asyncio.AbstractEventLoop | None = None
        self._device_check: asyncio.Task[Any] | None = None

    # ---------------------------------------------------------------- control
    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.service.recover_leases(all_sending=True)
        for coro in (self._dispatch_loop(), self._receipt_loop(), self._webhook_loop(), self._sweep_loop(),
                     self._inbound_loop(), self._cleanup_loop()):
            self._tasks.append(asyncio.create_task(coro))

    def check_devices(self) -> None:
        """Check every device with its provider (start-up, configuration change). Callable from any thread;
        a newer request replaces a check that is still running."""
        def begin() -> None:
            if self._stopping:
                return
            if self._device_check and not self._device_check.done():
                self._device_check.cancel()
            self._device_check = asyncio.create_task(check_devices(self.service))
        if self._loop is not None:
            self._loop.call_soon_threadsafe(begin)

    async def stop(self, grace: float = 10.0) -> None:
        self._stopping = True
        if self._device_check:
            self._device_check.cancel()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._inflight:
            # let provider calls that are already on the wire finish and record their outcome
            await asyncio.wait(self._inflight, timeout=grace)
        for t in self._inflight:
            t.cancel()

    def _beat(self, name: str) -> None:
        self.heartbeat[name] = time.monotonic()

    def heartbeat_age(self, name: str) -> float | None:
        t = self.heartbeat.get(name)
        return None if t is None else time.monotonic() - t

    async def _guard(self, name: str, fn: Any, interval: float) -> None:
        while True:
            self._beat(name)
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker loop error", extra={"worker": name})
                await asyncio.sleep(interval)

    # ------------------------------------------------------------- dispatcher
    async def _dispatch_loop(self) -> None:
        async def once() -> None:
            svc = self.service
            svc.wake.clear()
            svc.scheduler.release_due()
            svc.expire_due()
            try:
                svc.expire_reports()
            except Exception:  # never skip claim_due
                log.exception("report sweep failed")
            svc.recover_leases()
            svc.fail_orphaned()
            claimed_any = False
            for name, provider in svc.providers.items():
                free = provider.max_concurrency - self._busy.setdefault(name, 0)
                for d in svc.claim_due(name, free):
                    claimed_any = True
                    self._busy[name] += 1
                    task = asyncio.create_task(self._send_one(d))
                    self._inflight.add(task)
                    task.add_done_callback(self._inflight.discard)
            nxt = svc.next_due_in()
            # nothing claimable now (e.g. every provider busy): a finishing send sets `wake`, so don't spin
            timeout = 1.0 if nxt is None else min(1.0, max(0.05 if claimed_any else 1.0, nxt))
            try:
                await asyncio.wait_for(svc.wake.wait(), timeout)
            except TimeoutError:
                pass
        await self._guard("dispatcher", once, 1.0)

    async def _send_one(self, d: Any) -> None:
        svc = self.service
        started = now_iso()
        try:
            n = svc.db.one("SELECT * FROM notifications WHERE id=?", (d["notification_id"],))
            dev = svc.config.devices.get(d["device"])
            provider = svc.providers.get(d["provider"])
            if n is None:
                return
            if dev is None or provider is None:
                result = SendResult(outcome="permanent",
                                    error=f"device {d['device']!r} or provider {d['provider']!r} is no longer configured")
            else:
                msg = svc.message_for(n, d["id"], d["provider"], with_data=True, device=d["device"])
                try:
                    async with asyncio.timeout(svc.send_timeout(d["provider"])):  # always inside the lease
                        result = await provider.send(msg, dev.address)
                except TimeoutError:
                    result = SendResult(outcome="retryable",
                                        error="send timed out; outcome unknown, may be duplicated")
                except Exception as e:  # adapter bug: never lose the delivery
                    log.exception("provider send crashed", extra={"delivery_id": d["id"]})
                    result = SendResult(outcome="retryable", error=f"adapter error: {type(e).__name__}: {e}")
            svc.complete_send(d, result, started)
            if result.address_check is not None:
                record_check(svc, d["device"], result.address_check)
            if result.outcome == "success" and svc.cancel_requested_after_dispatch(d["id"]):
                await svc.cancel_after_dispatch(d["id"])
        except Exception:
            log.exception("dispatch failed", extra={"delivery_id": d["id"]})
        finally:
            self._busy[d["provider"]] = max(0, self._busy.get(d["provider"], 1) - 1)
            svc.wake.set()

    # ---------------------------------------------------------------- receipts
    async def _receipt_loop(self) -> None:
        async def once() -> None:
            svc = self.service
            for d in svc.due_polls():
                provider = svc.providers.get(d["provider"])
                if provider is None:  # never leave it in the queue: it would take a slot on every pass
                    svc.abandon_receipt(d, f"provider {d['provider']!r} is no longer configured")
                    continue
                try:
                    st = await provider.poll_receipt(d["receipt"])
                except ProviderError as e:
                    svc.poll_failed(d, str(e), e.http_status, e.body)
                    continue
                svc.apply_receipt(d["id"], st, via="receipt_poll")
                await asyncio.sleep(self.receipt_pause)
            await asyncio.sleep(self.receipt_pause)
        await self._guard("receipts", once, 5.0)

    # ------------------------------------------------------------------ replies
    async def _inbound_loop(self) -> None:
        failures: dict[str, int] = {}
        next_at: dict[str, float] = {}
        prints: dict[str, tuple] = {}

        async def once() -> None:
            start = time.monotonic()
            while time.monotonic() - start < self.inbound_interval:  # re-read each second: tests shorten it
                await asyncio.sleep(min(1.0, self.inbound_interval))
            svc = self.service
            reconcile_inbound(svc.providers, svc.inbound_status, failures, next_at, prints)
            for name, p in list(svc.providers.items()):
                if not getattr(p, "replies_ready", False) or time.monotonic() < next_at.get(name, 0.0):
                    continue
                status = svc.inbound_status.setdefault(name, {"last_success": None, "last_error": None})
                try:
                    mailbox = svc.open_mailbox(p) if svc.open_mailbox else p.mailbox()  # type: ignore[attr-defined]
                    status["last_stats"] = await poll_once(svc, p, mailbox)  # type: ignore[arg-type]
                    status.update(last_success=now_iso(), last_error=skipped_note(p))  # skipped UIDs stay visible
                    status["deleted_left"] = getattr(p, "_deleted_left", 0)  # moved mail a non-UIDPLUS server keeps
                    failures[name] = 0
                except Exception as e:
                    failures[name] = failures.get(name, 0) + 1
                    delay = min(900.0, 10.0 * 3 ** (failures[name] - 1))
                    next_at[name] = time.monotonic() + delay
                    err = mask_secret(f"{type(e).__name__}: {e}", getattr(p, "imap_password", "") or "")
                    status["last_error"] = err[:300]
                    log.warning("reply mailbox check failed",
                                extra={"provider": name, "error": status["last_error"], "retry_in": delay})
            svc.replies.sweep_unclaimed()
        await self._guard("inbound", once, 30.0)

    async def _cleanup_loop(self) -> None:
        """Automatic mailbox cleanup, in its own task so a long run never delays a poll (they share a lock per step)."""
        next_at: dict[str, float] = {}
        failures: dict[str, int] = {}

        async def once() -> None:
            waited = 0.0
            while waited < self.cleanup_check_seconds:  # re-read each second: tests shorten it
                step = min(1.0, self.cleanup_check_seconds)
                await asyncio.sleep(step)
                waited += step
            svc = self.service
            for n in [n for n in next_at if n not in svc.providers]:
                next_at.pop(n), failures.pop(n, None)
            for name, p in list(svc.providers.items()):
                if not getattr(p, "replies_ready", False) or not svc.config.cleanup_for(name).enabled:
                    continue
                now = self.clock()
                if now < next_at.setdefault(name, now + self.cleanup_start_delay):
                    continue
                try:
                    await svc.run_mailbox_cleanup(name, dry_run=False, by="hnl-notify", deadline_seconds=None)
                    failed = bool(svc.cleanup_status.get(name, {}).get("last_error"))
                except Exception as e:  # a bug must not turn into a retry on every pass
                    failed = True
                    err = mask_secret(f"{type(e).__name__}: {e}", getattr(p, "imap_password", "") or "")[:300]
                    svc.cleanup_status.setdefault(name, {}).update(last_error=err, last_run=now_iso())
                    log.warning("mailbox cleanup crashed", extra={"provider": name, "error": err})
                if failed:
                    failures[name] = failures.get(name, 0) + 1
                    next_at[name] = self.clock() + min(self.cleanup_backoff_max, 600.0 * 2 ** (failures[name] - 1))
                    log.warning("mailbox cleanup failed", extra={"provider": name, "retry_in": next_at[name] - self.clock()})
                else:
                    failures[name] = 0
                    next_at[name] = self.clock() + self.cleanup_interval
        await self._guard("cleanup", once, 30.0)

    # ---------------------------------------------------------------- webhooks
    async def _webhook_loop(self) -> None:
        sem = asyncio.Semaphore(4)

        async def deliver(item: dict[str, Any]) -> None:
            async with sem:
                body = dumps(item["event"]).encode()
                ts = int(time.time())
                headers = {"Content-Type": "application/json", "User-Agent": "hnl-notify",
                           "X-Notify-Event": item["event"]["type"], "X-Notify-Event-Id": str(item["event"]["id"]),
                           "X-Notify-Signature": sign(item["secret"], ts, body)}
                try:
                    if not await webhook_address_ok(item["url"], self.service.resolve):
                        self.service.webhook_result(item, False, None, "address not allowed")
                        return
                    resp = await (self.service.webhook_http or self.http).post(item["url"], content=body, headers=headers,
                                                timeout=self.service.config.server.webhook_timeout_seconds)
                    if attachments._peer_denied(resp):  # e.g. DNS changed between the check and the connect
                        self.service.webhook_result(item, False, None, "address not allowed")
                        return
                    ok = 200 <= resp.status_code < 300
                    self.service.webhook_result(item, ok, resp.status_code, None if ok else f"HTTP {resp.status_code}")
                except httpx.HTTPError as e:
                    # the stored text is shown to the subscription's owner: keep it coarse (no port-scan oracle)
                    log.info("webhook delivery failed", extra={"subscription_id": item["sid"],
                                                               "error": f"{type(e).__name__}: {e}"[:300]})
                    kind = ("address not allowed" if netguard.refused(e) else
                            "timeout" if isinstance(e, httpx.TimeoutException) else "connection failed")
                    self.service.webhook_result(item, False, None, kind)
                except Exception as e:  # e.g. a host name the resolver refuses: record it, never leave the row pending
                    log.info("webhook delivery failed", extra={"subscription_id": item["sid"],
                                                               "error": f"{type(e).__name__}: {e}"[:300]})
                    self.service.webhook_result(item, False, None, "invalid address")

        async def once() -> None:
            items = self.service.claim_webhooks(limit=20)
            if items:
                await asyncio.gather(*(deliver(i) for i in items))
            else:
                await self.service.bus.wait(1.0)
        await self._guard("webhooks", once, 2.0)

    # ------------------------------------------------------------------ sweeper
    async def _sweep_loop(self) -> None:
        last_retention = float("-inf")

        async def once() -> None:
            nonlocal last_retention
            if time.monotonic() - last_retention >= 3600:
                last_retention = time.monotonic()
                try:  # a failing retention must not stop the chat sweep below
                    removed = self.service.retention_sweep()
                    if removed:
                        log.info("retention sweep", extra={"removed": removed})
                except Exception:
                    log.exception("retention sweep failed; retried in an hour")
            self.service.chat.sweep()  # chat liveness must be checked well within STALE_SECONDS
            await asyncio.sleep(30)
        await self._guard("sweeper", once, 60.0)
