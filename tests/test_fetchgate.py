"""At most a few whole chat files are in memory at once: group_file, the REST download and the admin download share
one gate; a fetch over the limit waits its turn, and after the deadline it gets "busy, try again"."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from notify.fetchgate import FILE_FETCHES, Busy, FetchGate
from tests.test_admin import H  # noqa: F401
from tests.test_chat_admin import _join, ctx, fresh  # noqa: F401  (fixtures)
from tests.test_chat_files_admin import PNG, _up
from tests.test_chat_files_agents import chatlive  # noqa: F401  (fixture)


def test_gate_lets_slots_through_and_queues_the_rest():
    async def run() -> list[str]:
        gate = FetchGate(slots=2, wait=1.0)
        log: list[str] = []

        async def job(n: int) -> None:
            async with gate.slot():
                log.append(f"in{n}")
                await asyncio.sleep(0.05)
                log.append(f"out{n}")
        await asyncio.gather(*(job(n) for n in range(3)))
        return log
    log = asyncio.run(run())
    assert log.index("in2") > min(log.index("out0"), log.index("out1")), "the third waits for a free slot"


def test_gate_answers_busy_after_the_deadline():
    async def run() -> None:
        gate = FetchGate(slots=1, wait=0.05)
        async with gate.slot():
            with pytest.raises(Busy):
                async with gate.slot():
                    pass
    asyncio.run(run())


def test_a_slot_is_given_back_when_the_work_fails():
    async def run() -> bool:
        gate = FetchGate(slots=1, wait=0.05)
        with pytest.raises(ValueError):
            async with gate.slot():
                raise ValueError
        async with gate.slot():
            return True
    assert asyncio.run(run())


@pytest.fixture
def full_gate():
    old = (FILE_FETCHES.slots, FILE_FETCHES.wait)
    FILE_FETCHES.configure(slots=0, wait=0.05)  # nothing gets a slot: every fetch is busy
    yield
    FILE_FETCHES.configure(*old)


def test_admin_download_says_busy_when_the_gate_is_full(ctx, full_gate):
    srv, b, bearer = ctx
    gid = _join(srv, "host-a", "/srv/one", "s1")["group_id"]
    f = _up(b, gid, PNG).json()
    r = b.get(f"/admin/api/chats/files/{f['id']}")
    assert r.status_code == 503 and r.headers.get("retry-after")


def test_revalidation_needs_no_slot(ctx):
    srv, b, bearer = ctx
    gid = _join(srv, "host-a", "/srv/one", "s1")["group_id"]
    f = _up(b, gid, PNG).json()
    etag = b.get(f"/admin/api/chats/files/{f['id']}").headers["etag"]
    old = (FILE_FETCHES.slots, FILE_FETCHES.wait)
    FILE_FETCHES.configure(slots=0, wait=0.05)
    try:
        r = b.get(f"/admin/api/chats/files/{f['id']}", headers={"If-None-Match": etag})
    finally:
        FILE_FETCHES.configure(*old)
    assert r.status_code == 304, "an unchanged file is answered without loading it"


def test_rest_download_and_group_file_say_busy_when_the_gate_is_full(chatlive, full_gate):  # noqa: F811
    from tests.test_chat_files_agents import FakeFiles, call_tool, rest_join
    j = rest_join(chatlive)
    files = chatlive.service.chat.files = FakeFiles()
    files.put("cf_1", "a.log", b"hello", "text", "text/plain", None)
    r = chatlive.client("hosta").get("/v1/chat/files/cf_1", headers={"X-Member-Handle": j["member_handle"]})
    assert r.status_code == 503 and r.headers.get("retry-after")
    res = call_tool(chatlive, "bot", "group_file", {"member_handle": j["member_handle"], "file_id": "cf_1"})
    assert res.isError and "busy" in res.content[0].text
