"""Files in group chats: storage, what kind a file is, the per-chat cap, access, and what an agent is handed.

A file belongs to one message (`message_id`), or is "pending": uploaded by the owner and not posted yet. Bytes
live in the database like reply attachments and are purged (`data=NULL, purged_at`) by retention or by the cap;
the row stays so the message can say "file expired". The kind is decided from the bytes, never from what the
sender declared: only real PNG, JPEG, GIF and WebP are images.
"""

from __future__ import annotations

import hashlib
import io
import sqlite3
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from PIL import Image

from .attachments import clean_content_type, clean_filename
from .core import Invalid, NotFound
from .db import iso, new_id

if TYPE_CHECKING:
    from .chat import Chat

MIB = 1 << 20
MAX_FILES = 10  # per message
MESSAGE_MAX = 10 * MIB  # per message (owner), per file, and for one chat's pending uploads
AGENT_POST_MAX = 5 * MIB  # per message posted by an agent (inline over MCP)
CHAT_CAP = 100 * MIB  # unexpired files of one folder's chat, all its sessions together
PENDING_SECONDS = 3600  # an upload that was never posted goes after this
TEXT_PAGE = 100_000  # bytes of a text file handed to an agent per call
IMAGE_MAX_SIDE = 1568  # px on the long side handed to an agent; larger is scaled down
IMAGE_MAX_BYTES = 3_750_000
IMAGE_MAX_PIXELS = 40_000_000  # above this an image is never decoded (memory): the agent saves it to disk instead

GONE = "a file is no longer available: attach it again"
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "PNG", "image/png"), (b"\xff\xd8\xff", "JPEG", "image/jpeg"),
          (b"GIF87a", "GIF", "image/gif"), (b"GIF89a", "GIF", "image/gif"))
_TEXT_TYPES = frozenset({"application/json", "application/x-ndjson", "application/xml", "application/yaml",
                         "application/x-yaml", "application/toml", "application/x-patch", "application/x-diff",
                         "application/x-sh", "application/javascript"})
_UNKNOWN = "application/octet-stream"
_COLS = "id, message_id, group_id, idx, filename, content_type, size, kind, width, height, purged_at, created_at"


@dataclass
class NewFile:
    """A file as it arrives: nothing cleaned or checked yet."""
    filename: str | None
    content_type: str | None
    data: bytes


@dataclass
class StoredFile:
    id: str
    message_id: str | None
    group_id: str
    name: str
    type: str
    size: int
    kind: str
    width: int | None
    height: int | None
    data: bytes | None
    purged_at: str | None
    created_at: str

    @property
    def expired(self) -> bool:
        return self.data is None


@dataclass
class AgentView:
    """What group_file gives an agent: `note` always; `text` for a page of a text file; `image` (+ `image_type`)
    for a picture. kind is image, text, other (nothing to show: save it to disk) or expired."""
    kind: str
    note: str
    text: str | None = None
    image: bytes | None = None
    image_type: str | None = None


@dataclass
class _Prepared:
    name: str
    type: str
    kind: str
    width: int | None
    height: int | None
    data: bytes


def _image(data: bytes) -> tuple[str, int, int] | None:
    """(type, width, height) when the bytes are a PNG, JPEG, GIF or WebP that Pillow can read the header of."""
    fmt = next(((f, t) for magic, f, t in _MAGIC if data.startswith(magic)), None)
    if fmt is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        fmt = ("WEBP", "image/webp")
    if fmt is None:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=[fmt[0]]) as im:  # reads the header, decodes nothing
                w, h = im.size
    except Exception:  # noqa: BLE001  (anything Pillow cannot read is simply not an image here)
        return None
    return (fmt[1], w, h) if w > 0 and h > 0 else None


def _is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def sniff(filename: str | None, content_type: str | None, data: bytes) -> tuple[str, str, int | None, int | None]:
    """(kind, content type, width, height). An image gets its real type, whatever was declared."""
    img = _image(data)
    if img is not None:
        return ("image", *img)
    name = clean_filename(filename, "file")
    declared = clean_content_type(content_type, name)
    if declared.startswith("image/"):
        return ("other", declared if content_type else _UNKNOWN, None, None)  # says image, is not one we show
    textish = declared.startswith("text/") or declared in _TEXT_TYPES or declared == _UNKNOWN
    if textish and _is_text(data):
        return ("text", "text/plain" if declared == _UNKNOWN else declared, None, None)
    return ("other", declared, None, None)


def size_text(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < MIB:
        return f"{round(n / 1024)} KB"
    return f"{n / MIB:.1f} MB"


def summary(files: list[dict[str, Any]]) -> str:
    """What stands in for the text of a message that has only files: "2 files: a.png, b.log"."""
    return f"{len(files)} file{'' if len(files) == 1 else 's'}: " + ", ".join(f["name"] for f in files)


def frame_line(f: dict[str, Any]) -> str:
    """One file as a line for a frame. The name is the sender's text: the caller quotes and marker-strips it."""
    what = f"image {f['width']}x{f['height']}" if f["kind"] == "image" else f["kind"]
    return (f"[file {f['id']} · {f['name']} · {what} · {size_text(f['size'])}"
            + (" · expired" if f["expired"] else "") + "]")


def _out(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "name": r["filename"], "type": r["content_type"], "size": r["size"], "kind": r["kind"],
            "width": r["width"], "height": r["height"], "expired": r["purged_at"] is not None}


def _limit_text(n: int) -> str:
    return f"{n // MIB} MiB" if n % MIB == 0 else f"{n} bytes"


class ChatFiles:
    def __init__(self, chat: Chat):
        self.chat = chat

    @property
    def db(self):
        return self.chat.db

    # ------------------------------------------------------------------ taking files in
    @staticmethod
    def _prepare(new: NewFile, index: int) -> _Prepared:
        if not isinstance(new.data, (bytes, bytearray)) or len(new.data) == 0:
            raise Invalid(f"file {index + 1} is empty")
        data = bytes(new.data)
        name = clean_filename(new.filename, f"file-{index + 1}")
        kind, ctype, w, h = sniff(name, new.content_type, data)
        return _Prepared(name, ctype, kind, w, h, data)

    def prepare(self, files: list[NewFile]) -> list[_Prepared]:
        """Checks and sniffs an agent's files before the post's transaction (decoding headers is not for a lock)."""
        if len(files) > MAX_FILES:
            raise Invalid(f"at most {MAX_FILES} files per message")
        out = [self._prepare(f, i) for i, f in enumerate(files)]
        if sum(len(p.data) for p in out) > AGENT_POST_MAX:
            raise Invalid(f"files exceed {_limit_text(AGENT_POST_MAX)} in total")
        return out

    @staticmethod
    def _insert(c: sqlite3.Connection, p: _Prepared, group_id: str, message_id: str | None, idx: int,
                now: str) -> str:
        fid = new_id("cf")
        c.execute("INSERT INTO chat_files(id, message_id, group_id, idx, filename, content_type, size, sha256, kind,"
                  " width, height, data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (fid, message_id, group_id, idx, p.name, p.type, len(p.data), hashlib.sha256(p.data).hexdigest(),
                   p.kind, p.width, p.height, p.data, now))
        return fid

    def add_pending(self, group_id: str, new: NewFile) -> dict[str, Any]:
        """The owner's upload, before the message that will carry it. A chat holds at most one message's worth of
        pending files (10 files, 10 MiB): a newer upload pushes the oldest out, so an abandoned composer never
        blocks the next one."""
        if isinstance(new.data, (bytes, bytearray)) and len(new.data) > MESSAGE_MAX:
            raise Invalid(f"a file can be at most {_limit_text(MESSAGE_MAX)}")
        p = self._prepare(new, 0)
        with self.db.tx() as c:
            g = c.execute("SELECT state FROM chat_groups WHERE id=?", (group_id,)).fetchone()
            if g is None:
                raise NotFound("group not found")
            if g["state"] != "open":
                raise Invalid("group is closed")
            fid = self._insert(c, p, group_id, None, 0, iso(self.chat.now()))
            rows = c.execute("SELECT id, size FROM chat_files WHERE group_id=? AND message_id IS NULL"
                             " ORDER BY rowid DESC", (group_id,)).fetchall()
            count = total = 0
            for r in rows:  # newest first: keep while it fits, drop the rest
                count, total = count + 1, total + r["size"]
                if count > MAX_FILES or total > MESSAGE_MAX:
                    c.execute("DELETE FROM chat_files WHERE id=?", (r["id"],))
            row = c.execute(f"SELECT {_COLS} FROM chat_files WHERE id=?", (fid,)).fetchone()
        return _out(row)

    def discard_pending(self, group_id: str, file_id: str) -> None:
        with self.db.tx() as c:
            cur = c.execute("DELETE FROM chat_files WHERE id=? AND group_id=? AND message_id IS NULL",
                            (file_id, group_id))
            if cur.rowcount == 0:
                raise NotFound("file not found")

    def attach(self, c: sqlite3.Connection, g: sqlite3.Row, message_id: str, now: str, *,
               prepared: list[_Prepared] | None = None, pending: list[str] | None = None,
               ) -> tuple[list[dict[str, Any]], int]:
        """Inside the post's transaction: put the files on the message, then make room under the chat's cap.
        Returns (the files, how many older files were expired for the cap)."""
        if prepared:
            for i, p in enumerate(prepared):
                self._insert(c, p, g["id"], message_id, i, now)
        elif pending:
            if len(pending) > MAX_FILES:
                raise Invalid(f"at most {MAX_FILES} files per message")
            if len(set(pending)) != len(pending) or not all(isinstance(i, str) for i in pending):
                raise Invalid("each file can be attached once")
            total = 0
            for i, fid in enumerate(pending):
                r = c.execute("SELECT size FROM chat_files WHERE id=? AND group_id=? AND message_id IS NULL",
                              (fid, g["id"])).fetchone()
                if r is None:
                    raise Invalid(GONE)
                total += r["size"]
                c.execute("UPDATE chat_files SET message_id=?, idx=?, created_at=? WHERE id=?",
                          (message_id, i, now, fid))
            if total > MESSAGE_MAX:
                raise Invalid(f"files exceed {_limit_text(MESSAGE_MAX)} in total")
        rows = c.execute(f"SELECT {_COLS} FROM chat_files WHERE message_id=? ORDER BY idx", (message_id,)).fetchall()
        return [_out(r) for r in rows], self._make_room(c, g, message_id, now)

    @staticmethod
    def _make_room(c: sqlite3.Connection, g: sqlite3.Row, message_id: str, now: str) -> int:
        """The chat of a folder (all its sessions) keeps at most CHAT_CAP of unexpired files: the oldest go first,
        never those of the message being posted."""
        chat = "(SELECT id FROM chat_groups WHERE host=? AND folder=?)"
        where = f"group_id IN {chat} AND message_id IS NOT NULL AND data IS NOT NULL"
        total = c.execute(f"SELECT COALESCE(SUM(size), 0) FROM chat_files WHERE {where}",
                          (g["host"], g["folder"])).fetchone()[0]
        if total <= CHAT_CAP:
            return 0
        n = 0
        for r in c.execute(f"SELECT id, size FROM chat_files WHERE {where} AND message_id<>?"
                           " ORDER BY created_at, rowid", (g["host"], g["folder"], message_id)).fetchall():
            if total <= CHAT_CAP:
                break
            c.execute("UPDATE chat_files SET data=NULL, purged_at=? WHERE id=?", (now, r["id"]))
            total -= r["size"]
            n += 1
        return n

    # ------------------------------------------------------------------ cleaning up
    def sweep(self, c: sqlite3.Connection, now: datetime) -> None:
        """Uploads that were never posted."""
        c.execute("DELETE FROM chat_files WHERE message_id IS NULL AND created_at < ?",
                  (iso(now - timedelta(seconds=PENDING_SECONDS)),))

    # ------------------------------------------------------------------ reading
    def for_messages(self, message_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """The files of each message, in order; messages without files are left out."""
        out: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(message_ids), 500):
            part = message_ids[i:i + 500]
            for r in self.db.query(f"SELECT {_COLS} FROM chat_files WHERE message_id IN ({','.join('?' * len(part))})"
                                   " ORDER BY message_id, idx", tuple(part)):
                out.setdefault(r["message_id"], []).append(_out(r))
        return out

    def _get(self, file_id: str) -> StoredFile | None:
        r = self.db.one(f"SELECT {_COLS}, data FROM chat_files WHERE id=?", (file_id,)) if isinstance(file_id, str) else None
        if r is None:
            return None
        return StoredFile(r["id"], r["message_id"], r["group_id"], r["filename"], r["content_type"], r["size"],
                          r["kind"], r["width"], r["height"], r["data"], r["purged_at"], r["created_at"])

    def owner_get(self, file_id: str) -> StoredFile:
        """For the signed-in admin page: any file, pending ones too (the composer shows them)."""
        f = self._get(file_id)
        if f is None:
            raise NotFound("file not found")
        return f

    def member_get(self, member_handle: str, file_id: str) -> StoredFile:
        """A member reads the files of its own open group. Files of an earlier session in the same folder only
        for the member the owner approved group_history for, and only shortly after it read that history. Every
        other case is the same "not found"."""
        from .chat import HISTORY_TTL
        member = self.chat.member_from_handle(member_handle)
        f = self._get(file_id)
        if f is None or f.message_id is None:
            raise NotFound("file not found")
        if f.group_id == member["group_id"]:
            return f
        since = iso(self.chat.now() - HISTORY_TTL)
        ok = self.db.one(
            "SELECT 1 FROM chat_groups old JOIN chat_groups mine ON mine.host=old.host AND mine.folder=old.folder"
            " WHERE old.id=? AND old.state='closed' AND mine.id=? AND EXISTS (SELECT 1 FROM chat_history_requests h"
            " WHERE h.member_id=? AND h.state='used' AND h.used_at>=?)",
            (f.group_id, member["group_id"], member["id"], since))
        if ok is None:
            raise NotFound("file not found")
        return f

    # ------------------------------------------------------------------ what an agent is handed
    def for_agent(self, f: StoredFile, offset: int = 0, limit: int | None = None) -> AgentView:
        """Decodes images: call it from a worker thread."""
        if f.data is None:
            return AgentView("expired", f"file expired on {(f.purged_at or '')[:10]}")
        if f.kind == "text":
            return self._text(f, offset, limit)
        if f.kind == "image" and f.width and f.height:
            return self._image(f)
        return AgentView("other", f"{f.name} ({f.type}, {size_text(f.size)}) is not shown here; save it with: "
                                  f"notify-chat file {f.id}")

    @staticmethod
    def _text(f: StoredFile, offset: int, limit: int | None) -> AgentView:
        if not isinstance(offset, int) or offset < 0 or (limit is not None and (not isinstance(limit, int) or limit < 1)):
            raise Invalid("offset must be 0 or more and limit at least 1")
        data = f.data or b""
        n = len(data)
        start = min(offset, n)
        end = min(start + min(limit or TEXT_PAGE, TEXT_PAGE), n)
        if end < n:
            nl = data.rfind(b"\n", start, end)
            if nl >= 0:
                end = nl + 1
            else:  # one long line: cut between characters, never inside one
                cut = end
                while cut > start and (data[cut] & 0xC0) == 0x80:
                    cut -= 1
                if cut == start:  # the window is smaller than one character: finish that character
                    cut = end
                    while cut < n and (data[cut] & 0xC0) == 0x80:
                        cut += 1
                end = cut
        note = f"bytes {start}–{end} of {n}" + (f"; call again with offset={end}" if end < n else "")
        return AgentView("text", note, text=data[start:end].decode("utf-8", errors="replace"))

    @staticmethod
    def _image(f: StoredFile) -> AgentView:
        w, h = f.width or 0, f.height or 0
        data = f.data or b""
        if w * h > IMAGE_MAX_PIXELS:
            return AgentView("other", f"image {w}x{h} is too large to show; save it with: notify-chat file {f.id}")
        if max(w, h) <= IMAGE_MAX_SIDE and len(data) <= IMAGE_MAX_BYTES:
            return AgentView("image", f"image {w}x{h}", image=data, image_type=f.type)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                im = Image.open(io.BytesIO(data))
                im.seek(0)  # an animation: its first frame
                if im.format == "JPEG":
                    im.draft("RGB", (IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))  # decode at reduced size: less memory
                im.load()
            keep_png = f.type == "image/png"
            side = IMAGE_MAX_SIDE
            while True:
                small = im.copy()
                small.thumbnail((side, side), Image.Resampling.LANCZOS)
                out, ctype = _encode(small, keep_png)
                if len(out) > IMAGE_MAX_BYTES and keep_png:
                    out, ctype = _encode(small, False)
                if len(out) <= IMAGE_MAX_BYTES or side <= 256:
                    break
                side //= 2
        except Exception:  # noqa: BLE001  (a picture Pillow cannot decode after all)
            return AgentView("other", f"image {w}x{h} could not be scaled; save it with: notify-chat file {f.id}")
        nw, nh = small.size
        note = (f"image scaled from {w}x{h} to {nw}x{nh}" if (nw, nh) != (w, h)
                else f"image {w}x{h}, re-encoded to fit")
        return AgentView("image", note, image=out, image_type=ctype)


def _encode(im: Image.Image, as_png: bool) -> tuple[bytes, str]:
    buf = io.BytesIO()
    if as_png:
        if im.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            im = im.convert("RGBA")
        im.save(buf, "PNG", optimize=True)
        return buf.getvalue(), "image/png"
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))  # transparency over white
        flat.paste(rgba, mask=rgba.getchannel("A"))
        im = flat
    elif im.mode != "RGB":
        im = im.convert("RGB")
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue(), "image/jpeg"
