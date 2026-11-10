"""How many whole chat files may be in memory at once.

group_file (MCP), the REST download for `notify-chat file` and the admin page's download each load a whole file
(up to 10 MiB, more once base64'd for an image over MCP). One gate for all three bounds their sum; the image scaling
lock in chat_files only covers decoding. A fetch over the limit waits its turn; after `wait` seconds it gets Busy,
which the routes answer as 503 "busy, try again"."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

SLOTS = 2  # measured 2026-11-10, six agent flows at once: 3 slots peaked at 273-302 MiB RSS, 2 at 234-251 (cap 320M)
WAIT = 20.0


class Busy(Exception):
    """No slot within the deadline."""


class FetchGate:
    def __init__(self, slots: int = SLOTS, wait: float = WAIT):
        self.configure(slots, wait)

    def configure(self, slots: int, wait: float) -> None:
        self.slots, self.wait = slots, wait
        self._sems: dict[int, asyncio.Semaphore] = {}  # one per event loop (tests run several)

    def _sem(self) -> asyncio.Semaphore:
        key = id(asyncio.get_running_loop())
        if key not in self._sems:
            self._sems[key] = asyncio.Semaphore(self.slots)
        return self._sems[key]

    async def acquire(self) -> "Callable[[], Awaitable[None]]":
        """Take a slot; returns the async function that gives it back (for a response's background task, which
        runs after the body was sent)."""
        sem = self._sem()
        try:
            await asyncio.wait_for(sem.acquire(), self.wait)
        except TimeoutError:
            raise Busy("busy: too many files are being fetched; try again shortly") from None
        done = False

        async def release() -> None:
            nonlocal done
            if not done:  # once, however many paths call it
                done = True
                sem.release()
        return release

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        release = await self.acquire()
        try:
            yield
        finally:
            await release()


FILE_FETCHES = FetchGate()
BUSY_HEADERS = {"Retry-After": "5"}
