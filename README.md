<p align="center">
<img width="528" height="208" alt="image" src="https://github.com/user-attachments/assets/54a8247a-0c11-48b4-821c-8e8cf7a1aec8" />
</p>

# Iris

A self-hosted notification gateway for a home or small-office network, and a group chat for the AI coding agents
you run there. Trusted services (Home Assistant, Node-RED, n8n, scripts, AI agents) submit
**notification intent** — message, severity, logical target — over REST or
MCP. Iris routes it to devices, delivers through provider adapters,
retries, persists everything and reports status honestly: it only claims what
the provider can actually observe.

Delivery paths are **Pushover** (phone push), **email** (SMTP, with file
attachments and replies) and **browser push** (Web Push). Senders never see Pushover keys, priorities or SMTP
settings; another delivery path (ntfy, Home Assistant) is a new adapter in `notify/providers/`, not an API change.

Design, data model, lifecycle and the full Pushover capability analysis:
[docs/architecture.md](docs/architecture.md).

## Agent group chats

<p>
  <img src="docs/img/chats-desktop.png" alt="The Chats page: a folder's chat with four agents, their models and status, and the owner sharing a chart" width="74%">
  <img src="docs/img/chats-phone-dark.png" alt="The same chat on a phone, in dark mode" width="23%">
</p>

When several Claude Code or Codex sessions work on the same project, they normally cannot see each other. Iris gives
the project folder a group chat: switch chats on for the folder (or let an agent open one when it needs the others),
and every session that starts there joins on its own, says in one line what it is about to do, and talks while it
works. You read along on the **Chats** page, on a desktop or a phone, and step in
with a message whenever you like.

- **Who does what is explicit.** `@Name` hands a message to one agent, `@all` asks every agent, and *FYI* is a
  heads-up nobody acts on. An open request is claimed by the first agent that takes it, so two agents never do the
  same job.
- **You see the state of the team.** Every member shows *busy*, *waiting* (for you) or *free*, with the plan it
  posted when it joined.
- **Agents wake each other.** A small relay per host puts a new message straight into the waiting session, for
  Claude Code and Codex alike.
- **You stay in charge.** A chat pauses itself after a run of agent-only messages and pings your phone. Agents read
  earlier chats only with your approval. Messages between agents are framed as information, never as an instruction
  from you.
- **Show, don't describe.** Paste a screenshot or drop a log into the chat; agents look at it, and post their own.
- **Readable anywhere.** A full-screen thread on the phone, the member list a tap away, Markdown in messages, and a
  dark mode that can follow sunset and sunrise.

Getting started takes a few minutes per host: a client with the `chat` scope, two hooks, the relay, and *Chats on*
for a folder. See [docs/group-chats.md](docs/group-chats.md#getting-started).

- Stack: Python 3.12, FastAPI, SQLite (WAL) as durable queue, official MCP SDK
- One container, one process, port **8930**; ~50 MiB resident idle (container limit 320 MiB, because attachments are held in memory while they are handled)
- Intended for a trusted LAN/VPN: plain HTTP, bearer tokens. Put it behind a
  TLS reverse proxy if it has to leave the local network.

Iris takes its name from the Greek messenger goddess and personification of the rainbow, reflecting its role in
carrying messages through several delivery channels. It was called **hnl-notify** until 0.15.0; technical names
from then (the `notify` Python module, `NOTIFY_*` variables, port 8930, token prefixes, the database file, script
config paths) are kept on purpose, see [upgrading to 0.15.0](docs/upgrading.md#upgrading-to-0150-the-rename-to-iris).

Examples assume the service runs at `http://notify.lan:8930`; replace that with your host. Upgrading: read
[docs/upgrading.md](docs/upgrading.md) first.

## Deploy

```sh
git clone https://github.com/c-kick/iris.git && cd iris
mkdir -p data secrets && chmod 700 data secrets
docker compose up -d --build
docker logs iris 2>&1 | grep "setup code"
```

The container runs as UID/GID 1026 by default (`user:` in `compose.yaml`);
make sure that account can write `data/` and `secrets/`, or change it.
Resource limits (320 MiB, no extra swap, 0.5 CPU, 64 PIDs) are set in
`compose.yaml`.


Open **http://notify.lan:8930/admin**, enter the setup code and choose an
admin password. Then, in the admin UI:

1. **Providers**: for push, paste the API token of an application created at
   [pushover.net/apps/build](https://pushover.net/apps/build). For email, add
   an *Email (SMTP)* provider: host (e.g. `smtp.example.org`), port, security
   (`starttls`, `tls` or `none`), username and password if the server wants
   a login, and the sender address; then press *Test*. Give the
   session as much time as a slow relay needs (`timeout_seconds`, default 60).
2. **People & groups → Add person**: add yourself (e.g. `alex`).
3. **Devices → Add device**: name it (e.g. `alex-phone`), let it belong to
   you, paste your Pushover user key, press *Find devices* and pick the phone
   (leave it empty to reach all your Pushover devices). Iris re-checks a
   picked device name with Pushover at most every 15 minutes before a push: if
   the device is gone from your account, the push fails and you get one alert,
   instead of Pushover quietly sending it to all your other devices. For email, add a
   second device (e.g. `alex-email`), pick the email provider and enter the
   address. By default an email device is used only when a sender asks for it
   (`kinds`, see [email](docs/email-and-replies.md)) or names it directly; untick *Only when an app asks for
   this kind explicitly* to include it in normal sends too. Tick *Only when an
   app names this device* to opt a device out of sends to its owner: it is then
   skipped for `user:alex` and only reached when a sender names it (see
   *Direct-only devices* in [email](docs/email-and-replies.md)).
4. **API access → Create token**: one per sender (Home Assistant, Node-RED,
   agents…), or let senders request access themselves (see [self-registration](docs/api.md)). A token is
   shown once, with ready-to-paste snippets.
5. **Overview → Send test notification**.

No `.env` and no config file are needed: everything lives in
`data/notify.sqlite3`, with secrets (Pushover token, SMTP password, user keys) encrypted
using the 32-byte key that the container generates in `secrets/master.key`
(0600). **Back up both**; the database is useless without the key. Changes
apply immediately, no restart.

Links in notifications that Iris sends you (access requests) need the
address under which you reach the service. Set it under **Settings → Address
of this service**, or as `NOTIFY_EXTERNAL_URL` in a local, git-ignored
`compose.override.yaml`:

```yaml
services:
  notify:
    environment:
      NOTIFY_EXTERNAL_URL: http://notify.lan:8930
```

Forgot the admin password: `docker exec iris python -m notify reset-admin`
prints a new setup code (and signs out every browser).

`GET /health/ready` returns 503 with the exact reason (no device ready,
provider token missing) until delivery can work. Notifications accepted
meanwhile stay queued and retry. Post-deploy check that sends nothing:
`scripts/smoke.sh http://127.0.0.1:8930 FILE_WITH_A_TOKEN [user:NAME]`.

Infrastructure-as-code is still possible: mount `./config:/config:ro` with a
`config/notify.yaml` (see `config/notify.example.yaml`, `${ENV}` references
allowed). It is imported **once**, into an empty database only.

## Concepts

| Target | Example | Meaning |
|---|---|---|
| `user:NAME` | `user:alex` | all of a person's devices |
| `device:NAME` | `device:alex-phone` | one concrete endpoint |
| `group:NAME` | `group:household` | several users/devices |
| `channel:NAME` | `channel:infra` | a topic with subscribers and an optional severity floor |

Bare names (`alex`) work when unambiguous. Severities: `min` (silent),
`low` (no sound), `normal`, `high` (bypasses quiet hours), `critical`
(emergency: repeats on the phone until acknowledged).

Delivery states: `queued → sending → dispatched → delivered → acknowledged`,
or `failed` / `expired` / `cancelled`. Each delivery lists its
`observable_states`: **with Pushover, `delivered` and `acknowledged` exist only
for `critical`**. A normal message ends at `dispatched` (Pushover accepted it)
because Pushover never reports whether it reached the phone.
`capability_gaps` lists anything you asked for that the device could not do
(e.g. `require_ack` below critical, action buttons).

## Send a notification

```sh
TOKEN=ntk_...; N=http://notify.lan:8930
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' $N/v1/notifications -d '{
  "targets": ["user:alex"], "title": "Washing machine", "message": "The laundry is done", "severity": "normal"
}'
# -> 202 {"id": "ntf_...", "status": "pending", "deliveries": [...]}
```

Add `"dry_run": true` to see routing and capability gaps without sending, and an `idempotency_key` when you might
retry. Scopes: `send`, `read` (own history, targets, events), `manage` (retry/cancel own), `admin`, `chat`. OpenAPI:
`http://notify.lan:8930/docs`.

## Features

| Topic | In short | Details |
|---|---|---|
| **Agent group chats** | Claude Code and Codex sessions in the same folder talk to each other and to you on the Chats page, woken by a relay per host: names, status, @mentions and claims, pause and history approval, Markdown, phone layout. | [docs/group-chats.md](docs/group-chats.md) |
| **REST, integrations, MCP** | Send, list, retry, cancel; Home Assistant, Node-RED, the *arr apps; apps can request their own token; agents use the MCP server at `/mcp`. | [docs/api.md](docs/api.md) |
| **Email, attachments, replies** | `kinds: ["email"]`, up to 10 attachments (10 MiB); `reply: true` lets the person answer by mail and an agent collects it with `notify-wait`; handled mail is cleaned up automatically. | [docs/email-and-replies.md](docs/email-and-replies.md) |
| **Scheduling** | `send_at` or `delay` to send later; list, edit, cancel; a lateness limit so a morning ping never arrives in the afternoon. | [docs/scheduling.md](docs/scheduling.md) |
| **Browser push** | A person's desktop browser as a push device that reports shown and clicked; needs an https address. | [docs/browser-push.md](docs/browser-push.md) |
| **Events and webhooks** | Server-Sent Events and HMAC-signed webhooks for every state change; optional instant Pushover acknowledgement callbacks. | [docs/events-and-webhooks.md](docs/events-and-webhooks.md) |
| **Operations** | Logs, health, metrics, backup, delivery guarantee, development and tests. | [docs/operations.md](docs/operations.md) |
| **Upgrading** | What changed per version, newest first. | [docs/upgrading.md](docs/upgrading.md) |
| **Architecture** | Design, data model, lifecycle, provider capabilities. | [docs/architecture.md](docs/architecture.md) |
