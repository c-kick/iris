"""REST endpoints of the agent group chats: for the host hooks and the host relay (scope chat)."""

import asyncio
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .api import _errors, require, service
from .core import Client
from .models import Scope

router = APIRouter(prefix="/v1/chat", tags=["chat"])


class JoinIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(max_length=20)
    session_id: str = Field(max_length=200)
    folder: str = Field(max_length=4096)
    pid: int | None = None
    pid_start: str | None = Field(default=None, max_length=100)
    shared_process: bool = False  # 0.18.0: pid belongs to a process many sessions share (codex app-server)


class LeaveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="ended", max_length=20)


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(max_length=20)
    session_id: str = Field(max_length=200)
    folder: str | None = Field(default=None, max_length=4096)  # informational; the session id finds the member
    status: str = Field(max_length=20)
    model: str | None = Field(default=None, max_length=120)  # 0.21.0: the model the session runs, from its transcript
    tmux: str | None = Field(default=None, max_length=64)  # 0.21.0: where it runs in tmux, "#S:#I.#P"


class NotifyingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(max_length=20)
    session_id: str = Field(max_length=200)
    digest: str = Field(max_length=64)  # sha256 over the notification's title and message, never the text


class HeartbeatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alive: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)


@router.post("/presence", status_code=201)
@_errors
async def presence(body: JoinIn, request: Request, client: Client = Depends(require(Scope.chat))) -> dict[str, Any]:
    """A session starts: joined (chats on in this folder, or a group is open here) or present (chats off)."""
    return service(request).chat.presence(client, kind=body.kind, session_id=body.session_id, folder=body.folder,
                                          pid=body.pid, pid_start=body.pid_start, shared_process=body.shared_process)


@router.post("/join", status_code=201)
@_errors
async def join(body: JoinIn, request: Request, client: Client = Depends(require(Scope.chat))) -> dict[str, Any]:
    """Kept for hook scripts from before 0.14.0: the same as /presence (a member handle either way)."""
    return service(request).chat.presence(client, kind=body.kind, session_id=body.session_id, folder=body.folder,
                                          pid=body.pid, pid_start=body.pid_start, shared_process=body.shared_process)


@router.post("/leave", status_code=204)
@_errors
async def leave(request: Request, body: LeaveIn | None = None,
                handle: str = Header(alias="X-Member-Handle", max_length=200),
                client: Client = Depends(require(Scope.chat))) -> Response:
    service(request).chat.check_handle_host(client, handle)  # same host binding as the MCP tools
    service(request).chat.leave(handle, body.reason if body else "ended")
    return Response(status_code=204)


@router.post("/status", status_code=204)
@_errors
async def status(body: StatusIn, request: Request, client: Client = Depends(require(Scope.chat))) -> Response:
    """Status hooks: busy / waiting / free, or an "active" ping. 404 when the session is in no chat."""
    service(request).chat.set_status(client, kind=body.kind, session_id=body.session_id, status=body.status,
                                     model=body.model, tmux=body.tmux)
    return Response(status_code=204)


@router.post("/notifying", status_code=204)
@_errors
async def notifying(body: NotifyingIn, request: Request, client: Client = Depends(require(Scope.chat))) -> Response:
    """A session's tool hook, just before the session calls send_notification: when that notification arrives, the
    session's chat shows that it was sent. 404 when the session is in no chat."""
    service(request).chat.expect_notification(client, kind=body.kind, session_id=body.session_id, digest=body.digest)
    return Response(status_code=204)


@router.post("/heartbeat")
@_errors
async def heartbeat(body: HeartbeatIn, request: Request, client: Client = Depends(require(Scope.chat))) -> dict[str, Any]:
    return service(request).chat.heartbeat(client, body.alive)


@router.post("/wakes/claim")
@_errors
async def claim(request: Request, waiter: str = Query(min_length=1, max_length=100),
                wait: float = Query(default=25.0, ge=0, le=55),
                client: Client = Depends(require(Scope.chat))) -> Any:
    got = await service(request).chat.claim_host(client, waiter, wait)
    return Response(status_code=204) if got is None else got


@router.post("/wakes/{wake_id}/release", status_code=204)
@_errors
async def release(wake_id: str, request: Request, waiter: str = Query(min_length=1, max_length=100),
                  client: Client = Depends(require(Scope.chat))) -> Response:
    service(request).chat.release(client, wake_id, waiter)
    return Response(status_code=204)


@router.post("/wakes/{wake_id}/confirm", status_code=204)
@_errors
async def confirm(wake_id: str, request: Request, waiter: str = Query(min_length=1, max_length=100),
                  client: Client = Depends(require(Scope.chat))) -> Response:
    service(request).chat.confirm(client, wake_id, waiter)
    return Response(status_code=204)


@router.get("/files/{file_id}")
@_errors
async def chat_file(file_id: str, request: Request, handle: str = Header(alias="X-Member-Handle", max_length=200),
                    client: Client = Depends(require(Scope.chat))) -> Response:
    """The bytes of a chat file, for `notify-chat file <id>` on the member's host. The member handle travels in a
    header, never in the URL. Same access rule as the MCP tool group_file; always a download, never rendered."""
    if len(file_id) > 64:
        return Response(status_code=404)
    chat = service(request).chat
    chat.check_handle_host(client, handle)  # same host binding as the MCP tools
    from starlette.background import BackgroundTask
    from .fetchgate import BUSY_HEADERS, FILE_FETCHES, Busy
    try:
        release = await FILE_FETCHES.acquire()  # held until the body is sent
    except Busy as e:
        return Response(content=str(e), status_code=503, headers=BUSY_HEADERS)
    try:
        f = await asyncio.to_thread(chat.files.member_get, handle, file_id)
    except BaseException:
        await release()
        raise
    if f.expired or f.data is None:
        await release()
        return Response(status_code=410)
    return Response(content=f.data, media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(f.name or 'file', safe='')}",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", "X-Chat-File-Type": _header_safe(f.type)},
        background=BackgroundTask(release))


def _header_safe(value: str | None) -> str:
    return "".join(ch for ch in (value or "application/octet-stream") if 32 < ord(ch) < 127)[:100]
