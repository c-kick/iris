"""REST endpoints of the agent group chats: for the host hooks and the host relay (scope chat)."""

from typing import Any

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


class LeaveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="ended", max_length=20)


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(max_length=20)
    session_id: str = Field(max_length=200)
    folder: str | None = Field(default=None, max_length=4096)  # informational; the session id finds the member
    status: str = Field(max_length=20)


class HeartbeatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alive: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)


@router.post("/presence", status_code=201)
@_errors
async def presence(body: JoinIn, request: Request, client: Client = Depends(require(Scope.chat))) -> dict[str, Any]:
    """A session starts: joined (chats on in this folder, or a group is open here) or present (chats off)."""
    return service(request).chat.presence(client, kind=body.kind, session_id=body.session_id, folder=body.folder,
                                          pid=body.pid, pid_start=body.pid_start)


@router.post("/join", status_code=201)
@_errors
async def join(body: JoinIn, request: Request, client: Client = Depends(require(Scope.chat))) -> dict[str, Any]:
    """Kept for hook scripts from before 0.14.0: the same as /presence (a member handle either way)."""
    return service(request).chat.presence(client, kind=body.kind, session_id=body.session_id, folder=body.folder,
                                          pid=body.pid, pid_start=body.pid_start)


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
    service(request).chat.set_status(client, kind=body.kind, session_id=body.session_id, status=body.status)
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
