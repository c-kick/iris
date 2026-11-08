# Operations and development

## Operations

- Logs: `docker logs hnl-notify` (one JSON object per line; every state change is an `event` line with ids).
- Health: `/health/live`, `/health/ready` (includes `checks.scheduler`). Metrics (Prometheus text): `/metrics` (incl. `notify_scheduled_next_seconds`, `notify_scheduled_overdue`).
- Admin UI: `/admin` (session login, CSRF header, strict CSP). Admin API calls also accept a bearer token with the `admin` scope.
- Backup: `sqlite3 data/notify.sqlite3 ".backup data/notify-$(date +%F).sqlite3"` **plus** `secrets/master.key` (without it the stored Pushover and SMTP secrets cannot be decrypted). Backups include attachment bytes that have not yet been purged. History is kept 90 days (Settings).
- Signed-in admin page only: changing cleanup settings, *Clean up now* and reading reply contents need the browser session; an admin bearer token gets 403 on the first two and states, times and counts only for replies.
- Reply mailbox: *Providers* → *Manage…* in the admin UI (also linked from the size alert) shows the Processed and Quarantine folders, the cleanup
  settings and the last run; the poll, the connection test and cleanup share one lock per mailbox, so only one IMAP
  session of hnl-notify acts on it at a time.
- Reverse proxy: list its address in Settings → *Trusted reverse proxies* (`server.trusted_proxies`, signed-in page
  only). From those peers only, the client's address comes from `X-Forwarded-For` (the nearest hop that is not a trusted
  proxy) and the scheme from `X-Forwarded-Proto`; the login, decision-link and registration limits then count per
  client instead of per proxy. Requests through a proxy are never auto-approved for self-registration.
- Stopping: a relay or `notify-wait` long-poll cut off by a restart loses nothing: the same waiter gets its claim
  back first on its next call.
- Stopping: a stop during a mailbox poll or cleanup step waits for that step to finish (bounded by the 120 s IMAP session limit), so `compose.yaml` sets `stop_grace_period: 130s`.
- Delivery guarantee: at-least-once. If the container dies in the few ms after
  Pushover accepts a message but before the result is committed, that message
  is sent again after restart (logged as "send interrupted").

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -p no:warnings -o addopts=""   # addopts has -q, which hides the summary line
# on a shared host, cap it: systemd-run --user --scope -p MemoryMax=768M -p MemorySwapMax=0 -- .venv/bin/python -m pytest -o addopts=""
```

UI work without touching real Pushover: `.venv/bin/python scripts/dev_server.py
--dir /tmp/notify-dev --demo --password devpassword123` serves the app on
`127.0.0.1:18931` with a fake Pushover and demo data.

`aiosmtpd` (a dev dependency) provides a real local SMTP server for the email
tests. Tests start a real uvicorn server with a fake Pushover API (httpx
MockTransport) and exercise REST, SSE, webhooks, restart recovery and MCP via
the official SDK client. Adding a provider: subclass `notify.providers.base.Provider`
(capabilities, `plan`, `send`, optional receipts/cancel/callbacks) and
register it in `notify/providers/__init__.py`.

[← back to the README](../README.md)
