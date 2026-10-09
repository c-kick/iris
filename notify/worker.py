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
from .providers.base import ProviderError, SendResult

log = logging.getLogger("notify.worker")


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


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

    # ---------------------------------------------------------------- control
    def start(self) -> None:
        self.service.recover_leases(all_sending=True)
        for coro in (self._dispatch_loop(), self._receipt_loop(), self._webhook_loop(), self._sweep_loop()):
            self._tasks.append(asyncio.create_task(coro))

    async def stop(self, grace: float = 10.0) -> None:
        self._stopping = True
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
            svc.expire_due()
            svc.recover_leases()
            for name, provider in svc.providers.items():
                free = provider.max_concurrency - self._busy.setdefault(name, 0)
                for d in svc.claim_due(name, free):
                    self._busy[name] += 1
                    task = asyncio.create_task(self._send_one(d))
                    self._inflight.add(task)
                    task.add_done_callback(self._inflight.discard)
            nxt = svc.next_due_in()
            timeout = 1.0 if nxt is None else min(1.0, max(0.05, nxt))
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
                msg = svc.message_for(n, d["id"], d["provider"], with_data=True)
                try:
                    result = await provider.send(msg, dev.address)
                except Exception as e:  # adapter bug: never lose the delivery
                    log.exception("provider send crashed", extra={"delivery_id": d["id"]})
                    result = SendResult(outcome="retryable", error=f"adapter error: {type(e).__name__}: {e}")
            svc.complete_send(d, result, started)
            if result.outcome == "success" and result.receipt and svc.cancel_requested_after_dispatch(d["id"]):
                try:
                    await svc.cancel(Client(n["client_id"], frozenset()), n["id"])
                except Conflict:
                    pass
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
                if provider is None:
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
                    resp = await self.http.post(item["url"], content=body, headers=headers,
                                                timeout=self.service.config.server.webhook_timeout_seconds)
                    ok = 200 <= resp.status_code < 300
                    self.service.webhook_result(item, ok, resp.status_code, None if ok else f"HTTP {resp.status_code}")
                except httpx.HTTPError as e:
                    self.service.webhook_result(item, False, None, f"{type(e).__name__}: {e}")

        async def once() -> None:
            items = self.service.claim_webhooks(limit=20)
            if items:
                await asyncio.gather(*(deliver(i) for i in items))
            else:
                await self.service.bus.wait(1.0)
        await self._guard("webhooks", once, 2.0)

    # ------------------------------------------------------------------ sweeper
    async def _sweep_loop(self) -> None:
        async def once() -> None:
            removed = self.service.retention_sweep()
            if removed:
                log.info("retention sweep", extra={"removed": removed})
            await asyncio.sleep(3600)
        await self._guard("sweeper", once, 60.0)
