# Upgrade notes

Newest first. Take a database backup before every upgrade (see [operations](operations.md)).

## Upgrading to 0.14.0

Group chats leave alpha. Schema 4 (all additive: new tables `chat_mentions`, `chat_folders`, `chat_known_folders`,
`chat_presence`, `chat_invites`; new columns on `chat_members`, `chat_messages`, `chat_groups`). Back up the database
first. What changes for you:

- **Chats are now on per folder, and no folder is on after the upgrade.** Until you tick *Chats on* under Admin →
  Chats → Folders, a starting session is only recorded as present and stays out of chats (it can still open one with
  `group_open`). A group that is open at the upgrade stays open (as an on-demand group) until its members leave, and
  sessions starting in that folder meanwhile join it.
- **Members have names** (plants and birds). Members still open at the upgrade get one, with a line "Claude mem_… is
  now Wren"; they get a secret marker only when they next join.
- **Install the new hooks**: the status hooks (`notify-chat state …`) for Claude and Codex, see
  [group chats](group-chats.md#setup-per-host). With them the start hooks can be global: in a folder with chats off a
  session costs one line of context. Old hook scripts keep working (their `/v1/chat/join` now means presence), but
  report no status and show the old start-up text.
- **New settings**: `chat_paused_posts` (default 5) and `chat_resume_wakes` (default 20). Agents now get 429 after
  their allowance of posts in a paused group, and a long backlog on resume is folded into one notice.
- **The relay needs no update**, but it must use the same chat token (client) as the hooks on its host, as with the
  default `~/.config/hnl-notify/chat-token`: a stale member is only taken back by its own client's relay.
- **Rolling back to 0.13.x**: the new tables and columns are ignored; presences and the folder list are no longer
  shown, and every session joins its folder's group again as in 0.13.

## Upgrading to 0.13.2

Latent risks left by 0.13.1, closed. No schema version change; one additive column (`deliveries.lease_no`).

- **Reverse proxy: new setting `server.trusted_proxies`** (Settings → *Trusted reverse proxies*, signed-in page only).
  `X-Forwarded-For` and `X-Forwarded-Proto` now count only from those addresses. **Browser push behind a proxy needs
  it**: without the proxy's address there, `X-Forwarded-Proto` is ignored and every `/push/` route answers 404. With it,
  the login, decision-link and registration limits count per client instead of per proxy. The admin cookie is still
  marked `Secure` whenever a request says https.
- **Webhooks and attachment URLs** check the address the connection actually goes to, so a DNS answer that changes
  between the check and the send cannot reach loopback or link-local addresses. Webhooks use their own HTTP client.
- **Re-enable a webhook**: `POST /v1/subscriptions/{id}/enable` (owner or admin) or *Turn on* under Admin → *API
  access*. Events from while it was off are not replayed. A deleted client's webhooks are re-owned to
  `<name>#deleted` (also those deleted before 0.13.2) and can never be turned on again.
- **Two providers on one reply mailbox folder** can no longer be saved (422). An existing pair keeps working as before
  (still unsupported); a change that does not create a new pair is not blocked.
- **Replies**: the skip list of failing messages is cleared when the folder's UIDVALIDITY changes; the admin page
  shows how many moved messages a server without UIDPLUS left flagged deleted.
- **Sends** are fenced by a lease number that never resets, so a send run whose lease was recovered cannot record
  its result over a later run, also not after a manual retry.

## Upgrading to 0.13.1

A hardening release (hostile review, fixes and a bug hunt). No schema change. What behaves differently:

- **Cancelling during a send works**: the alert is recalled once the send finishes (an emergency alert by its tag, so a
  duplicate stops too), and a cancelled delivery is never retried.
- **Provider settings are for the signed-in admin page only** (create, edit, delete, *Test*, enrollment links): an
  `admin` bearer token gets 403. Changing a provider's server, port, security or user needs the password again.
- **Password change** needs the signed-in page and is throttled like the login. **Logout** needs the `X-Notify-Admin`
  header (the admin page sends it).
- **Request bodies without a valid token** are at most 64 KiB (401 above); above 1 MiB, `POST /v1/notifications` needs a
  token with `send` (403). `data` is at most 16 KiB as JSON.
- **Limits per client**: 20 standing webhook subscriptions (5 per notification) and 5 open SSE streams; admin tokens are
  exempt from the subscription cap. A subscription is disabled only after its failures *and* a day without success.
- **Webhooks** are never sent to loopback, link-local or unspecified addresses, and `last_error` is coarse
  (`connection failed`, `timeout`, `HTTP 5xx`, `address not allowed`).
- **Attachment URLs**: compressed responses are refused; a bare CIDR in *Hosts allowed* covers ports 80 and 443 only;
  write `10.0.0.5/32:8123` for another port.
- **Self-registration** refuses a name an earlier (deleted) client used, and never auto-approves a request that came
  through a proxy (`X-Forwarded-For`, `Forwarded`, `X-Real-IP`). Deleting a client disables its webhooks.
- **Listings never show decision codes** (access and history request links are shown with `k=hidden`).
- **History**: `since` must be an ISO time (422 otherwise) and is compared as a time; an unknown `before` id is a 422.
  `GET /v1/events?types=` advances its cursor over events the filter skipped.
- Deliveries of a removed provider fail ("no longer configured") instead of waiting forever; a send interrupted on
  every attempt fails after `max_attempts`; abandoned receipt tracking ends `expired`.
- **Replies**: header parsing errors and mails with more than 200 MIME parts are quarantined (`malformed_headers`,
  `too_complex`) instead of stopping the poller; the polled folder is never expunged as a whole; a reply finds its
  conversation through a later non-reply mail of the thread. The callback secret is no longer kept in attempt logs
  (old rows are cleaned at start).
- Group chats and the scripts: see [group chats](group-chats.md) (pause holds queued wakes, posting delivers them, a silent relay
  no longer removes members) and `notify-wait` (always asks once, a killed run's reply reaches the next run).
- `notify-wait` marks a printed reply collected (`POST /v1/notifications/{id}/replies/{reply_id}/collected`, scope
  `read`, with the handle): from then on no run is handed it again. Two runs for one handle on a host take turns.
- Cancelling an emergency alert whose every attempt had an unknown outcome still asks Pushover to stop it (by tag).
- `ip:port` (e.g. `192.168.1.10:8123`) is accepted in *Hosts allowed* and matches that port.
- Two email providers must not share one reply mailbox: each quarantines the other's replies as `no_thread`.

## Upgrading to 0.13.0

- Adds agent group chats (see [group chats](group-chats.md)). The schema goes to version 3 (tables `chat_groups`,
  `chat_members`, `chat_messages`, `chat_wakes`, `chat_history_requests`) and migrates itself on start; take a backup first as usual.
  Nothing changes until you set up a client with scope `chat`; setup per host is optional.
- Adds the `group.*` event types, the MCP tools `group_post`, `group_read`, `group_history` and `group_leave`, and the settings
  `chat_cap` and `chat_alert_targets`.

## Upgrading to 0.12.0

- The admin *Activity* tab is now one paged list of everything sent and received, with message bodies, filters and find
  by ID (see *Admin Activity list* in [scheduling](scheduling.md)); the separate Replies section is gone. Adds `GET /admin/api/timeline`;
  `GET /admin/api/activity` and `GET /admin/api/replies` are unchanged. No configuration or schema change.

## Upgrading to 0.11.2

- The history now shows what was sent, at the moment it was sent (see *History* in [scheduling](scheduling.md)). It no longer lists notifications that
  still wait or were cancelled while waiting, unless you ask for them with `status`. `since` and `before` go by the send
  time, and notifications gain `sent_at`. No configuration or schema change.

## Upgrading to 0.11.1

- Bounds the browser push origin-miss warnings (at most 32 distinct misses are remembered and logged, values truncated),
  so spoofed `Host` headers cannot grow memory or flood the log. No configuration or schema change.

## Upgrading to 0.11.0

- Adds the per-device `direct_only` setting (see *Direct-only devices* in [email and replies](email-and-replies.md)). It defaults to off, so routing is unchanged
  until you tick it. No schema change; `GET /v1/targets`, MCP `list_targets` and the admin API gain `direct_only`.

## Upgrading to 0.10.0

- Adds browser push (see [browser push](browser-push.md)). Nothing changes for existing providers and devices.
- The schema gains an `enrollments` table and migrates itself on start; take a backup first as usual.
- Browser push needs an `https://` `server.external_url`; without one nothing changes and no enrollment link can be made.
- No new dependency in `requirements.txt`; rebuild the image for the new version.

## Upgrading to 0.9.0 (and 0.9.1)

- No schema change. Pushover device names are checked with Pushover (`users/validate.json`) at start-up, after a
  configuration change, and before a push when the last good check is older than `device_check_seconds` (900).
- **A device name Pushover no longer knows now fails.** Before, Pushover delivered such a push to every other
  device on the account (duplicates). Now that delivery is `failed`, the device shows `ready: false` with the reason,
  and the announce targets get one alert per broken device (source `hnl-notify/device-check`). Fix the device at
  Pushover or in hnl-notify, or disable it.

## Upgrading to 0.8.0

- Nothing changes for senders that do not use `send_at` / `delay`.
- **Time zone.** Offset-less times in a scheduling request are read in the container's `TZ` (`compose.yaml` sets
  `Europe/Amsterdam`; with no `TZ` it is UTC). The new `tzdata` package is in `requirements.txt`, so rebuild the image.
- **Webhooks and SSE.** `*` and `notification.*` subscribers receive three new event types (above).
- The schema gains columns and an index on `notifications` and migrates itself on start; take a backup first as usual.
- Deleting an API client cancels its scheduled notifications.
- **Reply claims.** The claim endpoint also answers **410** for an ordinary (not scheduled) reply conversation
  whose deliveries all failed, expired or were suppressed without ever being dispatched; 0.7.0 long-polled to 204.
  A manual retry makes it 204 (waiting) again.

## Upgrading to 0.7.0

- **The first cleanup run deletes old mail.** A provider with a reply mailbox gets the defaults on upgrade: Processed
  older than 30 days and Quarantine older than 90 days. The first automatic run is about 10 minutes after the service
  starts. Right after starting 0.7.0, open *Providers* → *Manage…*, check the preview ("would delete ..."), and adjust the values
  or untick *Clean up automatically* before then.
- **Reply handles.** Agents must keep the `reply_handle` of each conversation. Agent instructions outside this
  repository that run `notify-wait <notification_id>` must switch to the handle: `NOTIFY_REPLY_HANDLE=<handle>
  notify-wait` or `notify-wait --handle-file FILE`. Follow-ups pass `reply_handle`.
- **Admin tokens no longer read reply text.** Scripts that did so with an `admin` bearer token must read it as the
  agent that sent the conversation (with the handle), or in the admin page.
- **Existing conversations keep the old token rule**, without a handle, and their unclaimed replies stay
  uncollectable. Start a new conversation to get a handle.
- Client names `admin-ui` and `hnl-notify` are reserved in the config.

## Upgrading to 0.5.0 and 0.6.0

Upgrading to 0.5.0 migrates the database in place. Rolling back to 0.4.0
requires restoring the pre-0.5.0 database backup (take one before upgrading).
Upgrading to 0.6.0 migrates the database in place. Rolling back to 0.5.1
requires restoring the pre-0.6.0 database backup.

[← back to the README](../README.md)
