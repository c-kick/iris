"""Email adapter: plain SMTP submission (smtplib, run in a worker thread).

Email reports acceptance by the first mail server and nothing else: no delivery or
read receipts, no acknowledgement, no recall.
"""

from __future__ import annotations

import asyncio
import html
import re
import smtplib
import socket
import ssl
import threading
import time
import unicodedata
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..models import INTERACTED, Severity
from .base import BASE_OBSERVABLE, DeliveryPlan, OutboundMessage, Provider, SendResult

MAX_SUBJECT_LINE = 120
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 10485760
REPLY_FOOTER = "Reply to this email to answer the agent."
_REPLY_KEYS = ("reply_address", "imap_host", "imap_user", "imap_password", "authserv_id")
_ADDR_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SECURITY = ("starttls", "tls", "none")
_KEYS = {"type", "host", "port", "security", "username", "password", "from_address", "from_name",
         "subject_prefix", "timeout_seconds", "verify_tls", "reply_address", "imap_host", "imap_port", "imap_user",
         "imap_password", "authserv_id", "reply_inbox", "reply_processed", "reply_quarantine"}


class _NoStartTLS(smtplib.SMTPNotSupportedError):
    """Our own STARTTLS-not-offered raise (other NotSupported errors come from smtplib, e.g. no AUTH)."""


def _header_safe(s: str | None) -> str:
    return re.sub(r"\s+", " ", "".join(" " if unicodedata.category(c).startswith("C") else c for c in s or "")).strip()


def _valid_address(s: str) -> bool:
    return bool(_ADDR_RE.match(s)) and not any(unicodedata.category(c).startswith("C") for c in s)


def _http(url: str | None) -> bool:
    try:
        return bool(url) and urlsplit(url).scheme in ("http", "https")
    except ValueError:
        return False


def _int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _links(msg: OutboundMessage) -> list[tuple[str, str]]:
    """(label, url) pairs: the message url, then every action that has one."""
    out = [(msg.url_title or "", msg.url)] if msg.url else []
    out += [(a.get("label") or a.get("id") or "", a["url"]) for a in msg.actions if a.get("url")]
    return out


class _SessionBox(list):
    """SMTP sessions the watchdog may close; one appended after expiry is closed at once."""

    def __init__(self, state: dict):
        super().__init__()
        self.state = state

    @staticmethod
    def kill(s: Any) -> None:
        sock = getattr(s, "sock", None)
        for op in (lambda: sock.shutdown(socket.SHUT_RDWR), lambda: sock.close()):
            try:
                op()
            except Exception:
                pass

    def append(self, s: Any) -> None:
        super().append(s)
        if self.state["expired"]:
            self.kill(s)


class EmailProvider(Provider):
    type = "email"
    kind = "email"
    label = "Email (SMTP)"
    max_concurrency = 1
    secret_settings = frozenset({"password", "imap_password"})
    settings_schema = {
        "host": {"label": "SMTP server", "kind": "text", "help": "Host name of your mail server or relay, e.g. smtp.example.org."},
        "port": {"label": "Port", "kind": "int", "default": 587},
        "security": {"label": "Encryption", "kind": "select", "default": "starttls",
                     "options": [["starttls", "STARTTLS (usually port 587)"], ["tls", "TLS from the start (usually port 465)"],
                                 ["none", "None (local relay only)"]]},
        "username": {"label": "Username", "kind": "text", "help": "Leave empty if the server needs no login."},
        "password": {"label": "Password", "kind": "secret"},
        "from_address": {"label": "From address", "kind": "text"},
        "from_name": {"label": "From name", "kind": "text", "default": "Iris"},
        "subject_prefix": {"label": "Subject prefix", "kind": "text", "help": "Optional, e.g. [home]."},
        "timeout_seconds": {"label": "Give up a send after (seconds)", "kind": "int", "default": 60},
        "verify_tls": {"label": "Check the server's TLS certificate", "kind": "check", "default": True},
        "reply_address": {"label": "Reply mailbox address", "kind": "text",
                          "help": "Optional. Set this and the IMAP fields to let people answer agents by replying, e.g. replies@example.org."},
        "imap_host": {"label": "Reply mailbox IMAP server", "kind": "text", "help": "TLS from the start (IMAPS)."},
        "imap_port": {"label": "IMAP port", "kind": "int", "default": 993},
        "imap_user": {"label": "IMAP username", "kind": "text"},
        "imap_password": {"label": "IMAP password", "kind": "secret"},
        "authserv_id": {"label": "Mail server name in Authentication-Results", "kind": "text",
                        "help": "The name your mail server writes first in the Authentication-Results header it adds. Test connection shows what it sees."},
        "reply_inbox": {"label": "Folder to read", "kind": "text", "default": "INBOX"},
        "reply_processed": {"label": "Folder for handled replies", "kind": "text", "default": "Processed"},
        "reply_quarantine": {"label": "Folder for rejected mail", "kind": "text", "default": "Quarantine"},
    }
    address_schema = {
        "to": {"label": "Email address", "kind": "text", "help": "One address; make several devices for several addresses."},
    }

    def __init__(self, name: str, settings: dict[str, Any], http: httpx.AsyncClient):
        super().__init__(name, settings, http)
        self.host = str(settings.get("host") or "").strip()
        self.port = _int(settings.get("port", 587), 0)
        self.security = str(settings.get("security") or "starttls")
        self.username = str(settings.get("username") or "")
        self.password = str(settings.get("password") or "")
        self.from_address = str(settings.get("from_address") or "").strip()
        self.from_name = str(settings.get("from_name") if settings.get("from_name") is not None else "Iris")
        self.subject_prefix = str(settings.get("subject_prefix") or "")
        self.timeout = max(10, min(300, _int(settings.get("timeout_seconds", 60), 60)))
        self.verify_tls = bool(settings.get("verify_tls", True))
        self.send_deadline_seconds = float(self.timeout)
        self.reply_address = str(settings.get("reply_address") or "").strip()
        self.imap_host = str(settings.get("imap_host") or "").strip()
        self.imap_port = _int(settings.get("imap_port", 993), 0)
        self.imap_user = str(settings.get("imap_user") or "")
        self.imap_password = str(settings.get("imap_password") or "")
        self.authserv_id = str(settings.get("authserv_id") or "").strip()
        self.reply_inbox = str(settings.get("reply_inbox") or "INBOX")
        self.reply_processed = str(settings.get("reply_processed") or "Processed")
        self.reply_quarantine = str(settings.get("reply_quarantine") or "Quarantine")
        self._unknown = sorted(set(settings) - _KEYS)

    # --- configuration ---
    def config_problems(self) -> list[str]:
        problems = []
        if not self.host:
            problems.append("host is not set")
        if not self.from_address:
            problems.append("from_address is not set")
        elif not _valid_address(self.from_address):
            problems.append("from_address is not a valid email address")
        if self.security not in _SECURITY:
            problems.append(f"security must be one of {', '.join(_SECURITY)}")
        elif self.security == "none" and self.username:
            problems.append("login over an unencrypted connection would expose the password; use STARTTLS or TLS")
        if not 1 <= self.port <= 65535:
            problems.append("port must be between 1 and 65535")
        if self._unknown:
            problems.append(f"unknown settings: {', '.join(self._unknown)}")
        return problems

    def device_problems(self, address: dict[str, Any]) -> list[str]:
        to = str(address.get("to") or "")
        if not to:
            return ["address.to is not set"]
        if not _valid_address(to):
            return ["address.to is not a valid email address"]
        return []

    def recipient(self, address: dict[str, Any]) -> str | None:
        return str(address.get("to") or "").strip().lower() or None

    def replies_problems(self) -> list[str]:
        """Why replies are off; never part of config_problems, so ordinary email keeps working."""
        if not any(getattr(self, k) for k in _REPLY_KEYS):
            return ["reply mailbox not configured"]
        problems = [f"{k} is not set" for k in _REPLY_KEYS if not getattr(self, k)]
        if self.reply_address and not _valid_address(self.reply_address):
            problems.append("reply_address is not a valid email address")
        if not 1 <= self.imap_port <= 65535:
            problems.append("imap_port must be between 1 and 65535")
        return problems

    @property
    def replies_ready(self) -> bool:
        return not self.replies_problems()

    def message_id(self, delivery_id: str) -> str:
        return f"<{delivery_id}@{self.from_address.rpartition('@')[2]}>"

    def mailbox(self) -> Any:
        from .email_inbound import ImapMailbox
        return ImapMailbox.for_provider(self)

    # --- capabilities ---
    def capabilities(self) -> dict[str, Any]:
        return {
            "accept_confirmation": True,
            "delivery_receipt": {"supported": False},
            "read_receipt": {"supported": False},
            "acknowledgement": {"supported": False},
            "interactions": {"supported": False},
            "actions": {"max": 0, "inline_links": True,
                        "note": "no buttons; actions with a url become links in the message; taps are not reported"},
            "attachments": {"max_count": MAX_ATTACHMENTS, "max_bytes": MAX_ATTACHMENT_BYTES, "types": ["*/*"]},
            "links": {"max": 10, "note": "shown in the message body, not reported back"},
            "cancel_after_dispatch": {"supported": False},
            "ttl": {"supported": False, "note": "email cannot be recalled; expiry applies only before sending"},
            "replies": {"supported": self.replies_ready,
                        "note": None if self.replies_ready else "; ".join(self.replies_problems())},
            "severities": [s.value for s in Severity],
            "max_title_length": None,
            "max_message_length": None,
        }

    def plan(self, msg: OutboundMessage, address: dict[str, Any]) -> DeliveryPlan:
        gaps: list[str] = []
        if msg.require_ack:
            gaps.append("acknowledgement: email cannot report acknowledgement; not tracked")
        for a in msg.actions:
            if not a.get("url"):
                gaps.append(f"action {a['id']!r} dropped (no url)")
            elif not _http(a["url"]):
                gaps.append(f"action {a['id']!r} shown as text (url is not http/https)")
        if msg.url and not _http(msg.url):
            gaps.append("url shown as text (not http/https)")
        observable = list(BASE_OBSERVABLE) + ([INTERACTED] if msg.reply and self.replies_ready else [])
        return DeliveryPlan(tracking="none", observable_states=observable, capability_gaps=gaps)

    # --- message ---
    def build_message(self, msg: OutboundMessage, address: dict[str, Any]) -> EmailMessage:
        out = EmailMessage(policy=policy.SMTP)
        first = (msg.message.splitlines() or [""])[0][:MAX_SUBJECT_LINE]
        out["Subject"] = _header_safe(f"{_header_safe(self.subject_prefix)} {_header_safe(msg.title) or first}")
        out["From"] = formataddr((_header_safe(self.from_name), self.from_address))
        out["To"] = str(address.get("to") or "")
        out["Date"] = format_datetime(msg.created_at)
        out["Message-ID"] = self.message_id(msg.delivery_id)
        if msg.thread_delivery_ids:
            refs = [self.message_id(i) for i in msg.thread_delivery_ids]
            out["In-Reply-To"] = refs[-1]
            out["References"] = " ".join(refs)
        if msg.auto_submitted:
            out["Auto-Submitted"] = "auto-replied"
        reply_on = msg.reply and self.replies_ready
        if reply_on:
            out["Reply-To"] = self.reply_address
        out["X-Notify-Notification"] = _header_safe(msg.notification_id)
        if msg.severity in (Severity.high, Severity.critical):
            out["Importance"] = "high"
            out["X-Priority"] = "1"
        elif msg.severity in (Severity.min, Severity.low):
            out["Importance"] = "low"
        links = _links(msg)
        text = msg.message
        if links:
            text += "\n\n" + "\n".join(f"{label}: {url}" if label else url for label, url in links)
        if reply_on:
            text += "\n\n" + REPLY_FOOTER
        parts = ['<div style="font-family:sans-serif">', html.escape(msg.message).replace("\r\n", "\n").replace("\n", "<br>")]
        for label, url in links:
            if _http(url):
                parts.append(f'<p><a href="{html.escape(url, quote=True)}" style="display:inline-block;padding:8px 14px;'
                             f'background:#2563eb;color:#fff;border-radius:6px;text-decoration:none">'
                             f'{html.escape(label or url)}</a></p>')
            else:
                parts.append(f"<p>{html.escape(f'{label}: {url}' if label else url)}</p>")
        if reply_on:
            parts.append(f'<p style="color:#666;font-size:90%">{html.escape(REPLY_FOOTER)}</p>')
        parts.append("</div>")
        out.set_content(text)
        out.add_alternative("".join(parts), subtype="html")
        for a in msg.attachments:
            if a.data is None:
                continue
            ct = a.content_type if re.fullmatch(r"[\w.+-]+/[\w.+-]+", a.content_type or "") else "application/octet-stream"
            maintype, subtype = ct.split("/", 1)
            out.add_attachment(a.data, maintype=maintype, subtype=subtype, filename=_header_safe(a.filename) or "attachment")
        return out

    # --- delivery ---
    def _open(self, left: Any, box: list) -> smtplib.SMTP:
        """Connect, EHLO and (if configured) STARTTLS; the session is appended to `box` for the watchdog."""
        ctx = ssl.create_default_context()
        if not self.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        s = smtplib.SMTP_SSL(self.host, self.port, timeout=left(), context=ctx) if self.security == "tls" \
            else smtplib.SMTP(self.host, self.port, timeout=left())
        box.append(s)
        s.sock.settimeout(left())  # the constructor may have used up the budget
        s.ehlo()
        if self.security == "starttls":
            if not s.has_extn("starttls"):
                raise _NoStartTLS("server does not offer STARTTLS")
            s.sock.settimeout(left())
            s.starttls(context=ctx)
            s.ehlo()
        return s

    def _login(self, s: smtplib.SMTP, left: Any) -> None:
        if self.username:
            s.sock.settimeout(left())
            s.login(self.username, self.password)

    @staticmethod
    def _size(s: smtplib.SMTP) -> int:
        try:
            return max(0, int(s.esmtp_features.get("size") or 0))
        except (TypeError, ValueError):
            return 0

    def _deadline(self) -> Any:
        end = time.monotonic() + self.timeout

        def left() -> float:
            r = end - time.monotonic()
            if r <= 0:
                raise TimeoutError(f"SMTP session exceeded {self.timeout} s")
            return r
        return left

    def _watchdog(self) -> tuple[threading.Timer, list, dict]:
        """Hard session deadline: closes the socket so a stalled phase cannot outlive timeout_seconds."""
        state = {"expired": False}
        box = _SessionBox(state)

        def expire() -> None:
            state["expired"] = True
            for s in list(box):
                box.kill(s)
        t = threading.Timer(self.timeout, expire)
        t.daemon = True
        return t, box, state

    def _error(self, e: Exception, state: dict) -> SendResult:
        if state["expired"] and not isinstance(e, TimeoutError):
            e = TimeoutError(f"SMTP session exceeded {self.timeout} s")
        return self._map_error(e)

    @staticmethod
    def _quit(s: smtplib.SMTP) -> None:
        try:
            s.quit()
        except Exception:
            pass

    def _send_sync(self, raw: bytes, sender: str, to: str) -> SendResult:
        left = self._deadline()
        timer, box, state = self._watchdog()
        timer.start()
        try:
            s = self._open(left, box)
            lim = self._size(s)
            if lim and len(raw) > lim:
                self._quit(s)
                return SendResult(outcome="permanent",
                                  error=f"message is {len(raw) / 1048576:.1f} MB, server accepts at most {lim / 1048576:.1f} MB")
            self._login(s, left)
            s.sock.settimeout(left())
            s.sendmail(sender, [to], raw)
            self._quit(s)
            return SendResult(outcome="success")
        except Exception as e:
            return self._error(e, state)
        finally:
            timer.cancel()
            for s in box:
                s.close()

    @staticmethod
    def _map_error(e: Exception) -> SendResult:
        if isinstance(e, smtplib.SMTPAuthenticationError):
            return SendResult(outcome="retryable", retry_after=900,
                              error="login rejected; check username/password in the admin UI")
        if isinstance(e, _NoStartTLS):
            return SendResult(outcome="retryable",
                              error="server does not offer STARTTLS; choose another Encryption setting")
        if isinstance(e, smtplib.SMTPNotSupportedError):
            return SendResult(outcome="retryable", error=f"server does not support: {e}")
        if isinstance(e, (smtplib.SMTPConnectError, smtplib.SMTPHeloError, smtplib.SMTPServerDisconnected)):
            return SendResult(outcome="retryable", error=f"{type(e).__name__}: {e}")
        code: int | None = None
        text = str(e)
        if isinstance(e, smtplib.SMTPRecipientsRefused):
            first = next(iter(e.recipients.values()), None)
            if first:
                code = first[0]
                text = f"{first[0]} {first[1].decode(errors='replace') if isinstance(first[1], bytes) else first[1]}"
        elif isinstance(e, smtplib.SMTPResponseException):
            code = e.smtp_code
            text = f"{e.smtp_code} {e.smtp_error.decode(errors='replace') if isinstance(e.smtp_error, bytes) else e.smtp_error}"
        if code is not None:
            return SendResult(outcome="permanent" if code >= 500 else "retryable", error=text)
        return SendResult(outcome="retryable", error=f"{type(e).__name__}: {e}")

    async def send(self, msg: OutboundMessage, address: dict[str, Any]) -> SendResult:
        problems = self.config_problems() + self.device_problems(address)
        if problems:
            return SendResult(outcome="retryable", error="; ".join(problems))
        to = str(address["to"])
        return await asyncio.to_thread(self._deliver, msg, address, to)

    def _deliver(self, msg: OutboundMessage, address: dict[str, Any], to: str) -> SendResult:
        """Build (MIME + base64 of large attachments is CPU and memory heavy) and send, off the event loop."""
        try:
            em = self.build_message(msg, address)
            raw = em.as_bytes()
        except Exception as e:
            return SendResult(outcome="permanent", error=f"could not build the message: {type(e).__name__}: {e}")
        message_id, subject_chars = str(em["Message-ID"]), len(str(em["Subject"]))
        del em
        summary = {"host": self.host, "port": self.port, "security": self.security,
                   "to_domain": "…@" + to.rpartition("@")[2], "subject_chars": subject_chars,
                   "attachments": sum(1 for a in msg.attachments if a.data is not None),
                   "attachment_bytes": sum(len(a.data) for a in msg.attachments if a.data is not None),
                   "encoded_bytes": len(raw)}
        for a in msg.attachments:
            a.data = None  # the encoded message holds them now; a retry reloads from the database
        res = self._send_sync(raw, self.from_address, to)
        res.request_summary = summary
        if res.outcome == "success":
            res.provider_message_id = message_id
        return res

    # --- connection test ---
    def _test_sync(self) -> dict:
        left = self._deadline()
        timer, box, state = self._watchdog()
        timer.start()
        try:
            s = self._open(left, box)
            lim = self._size(s)
            self._login(s, left)
            s.sock.settimeout(left())
            s.noop()
            self._quit(s)
            return {"supported": True, "ok": True, "tls": self.security, "size_limit": lim or None}
        except Exception as e:
            err = self._error(e, state).error or f"{type(e).__name__}: {e}"
            if len(self.password) >= 4:
                err = err.replace(self.password, "***")
            return {"supported": True, "ok": False, "error": err}
        finally:
            timer.cancel()
            for s in box:
                s.close()

    async def test_connection(self) -> dict:
        problems = self.config_problems()
        if problems:
            return {"supported": True, "ok": False, "error": "; ".join(problems)}
        out = await asyncio.to_thread(self._test_sync)
        if any(getattr(self, k) for k in _REPLY_KEYS):
            rp = self.replies_problems()
            out["imap"] = {"ok": False, "error": "; ".join(rp)} if rp else await asyncio.to_thread(self.mailbox().test)
        return out
