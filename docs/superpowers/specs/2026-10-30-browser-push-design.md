# Browser push (Web Push) — design

Date: 2026-10-30 · Status: revision 2 (after builder check and owner answers) · Target version: 0.10.0 (on top of 0.9.1)

## Intent

Desktop push currently goes through paid Pushover desktop clients. When those licences lapse,
the device names disappear from the Pushover account, and (before 0.9.0) the pushes for them
quietly landed on the phone as duplicates. Browsers have a free, standard push mechanism
(Web Push: RFC 8030 delivery, RFC 8291 encryption, RFC 8292 VAPID). hnl-notify gets a **browser
provider**, so a desktop browser (Chrome, Edge, Firefox) becomes a notification device like a
Pushover phone or a mailbox, at no cost.

Beyond being free, the browser can do something Pushover cannot. It is on the same LAN as
hnl-notify, so it can report that a notification was **shown** and **clicked**. Agents then see
`delivered` and `acknowledged` for ordinary messages, not only for critical Pushover alarms.

## Goals

1. A new provider type `webpush` (kind `push`), with devices that are browser subscriptions.
   Devices belong to a person, so `user:NAME` reaches them like any other push device; severity
   floors, `enabled`, attention, retries, the delivery log and the attempt log all work as usual.
2. **Enrollment by one-time link.** In the admin UI the owner creates an enrollment link for a
   person. Opening it in a browser shows one *Enable notifications on this browser* button. The
   browser asks for permission and subscribes, and hnl-notify stores the subscription as a new
   device of that person. The device name follows the usual name rule (e.g. `alex-browser-2`);
   the human label from the user agent ("Edge on Windows") goes in `description`, editable. The link works once and expires (default 24 h). It needs no admin login.
3. **Click opens the notification's link** (`url`, else the first URL action). With no link,
   a click only closes the notification (and is still reported).
4. **Shown and clicked are reported back.** The service worker reports *shown* (when it displays
   the notification) and *clicked* to hnl-notify. The delivery moves `dispatched → delivered`
   when shown, and to `acknowledged` when clicked (`acknowledged_by` = device name). Both are
   in `observable_states` for webpush deliveries at every severity.
   **Ending without a report (owner decision):** every push has a lifetime: the notification's
   expiry, else **24 h**. That lifetime is the `TTL` header, and when it passes without a
   *shown* report the delivery becomes `expired`. A delivery that was shown but not clicked
   stays `delivered`. Only `high`/`critical`/`require_ack` webpush deliveries appear under
   *awaiting acknowledgement* in attention (until clicked or the lifetime passes, then
   `expired` like an unacknowledged Pushover emergency); ordinary ones never do. `expired`
   is in `observable_states`.
   **Per device (owner decision):** a click acknowledges only that browser's delivery. It does
   not cancel the Pushover emergency or close the notification on other devices.
5. **Severity.** `high` and `critical` notifications stay on screen until clicked or dismissed
   (`requireInteraction`); lower ones follow the OS default. Browsers cannot repeat a sound like
   Pushover's emergency alarm, so there is no re-alerting. `require_ack` is satisfied by a click.
6. **Dead subscriptions are detected.** When a push service answers 404 or 410 (the subscription
   is gone: browser reset, permission revoked, unsubscribed), the delivery fails permanently,
   the device is flagged as a problem (`ready: false`), and the announce targets get **one
   alert**. This reuses the 0.9.0 device-check mechanism (`AddressCheck` on the send result,
   `device_check.record_check`, the `meta` flag); there is nothing to poll ahead of time.
7. **Content.** Title, message, severity, link and expiry as for Pushover. **No images (owner
   decision):** a push payload is limited to about 4 KiB and serving attachment bytes to the
   browser would need an extra no-login endpoint, so every attachment is a capability gap
   (`attachments.max_count: 0`). The `TTL` header carries the lifetime (goal 4), so a browser
   that was closed does not show stale pushes later.
8. **LAN only.** hnl-notify is never exposed to the internet. The browser reaches hnl-notify
   (enrollment page, service worker, reports) over HTTPS through an existing LAN-only reverse
   proxy route. The only traffic beyond the LAN is hnl-notify's **outbound** POST to the
   browser vendor's push service (FCM for Chrome/Edge, Mozilla autopush for Firefox, WNS
   behind Edge), which then delivers to the browser. Nothing is opened inbound.

## Non-goals

- iPhone/Safari web push. It needs a Home Screen web app; the phone stays on Pushover for now.
  Nothing in the design should rule it out later.
- Action buttons in the notification (Approve/Deny). Click opens the link; extra URL actions
  stay links (inside the notification text where possible, otherwise a capability gap).
- Re-sending or repeating critical alerts until clicked.
- Reachability from outside the LAN/VPN, and any public endpoint.
- Cross-device acknowledgement (a click ending the same notification elsewhere).
- Images or other attachments in browser notifications.
- Email/Pushover behaviour changes.

## Anti-goals (would make this a failure even if it works)

- Any route that makes hnl-notify reachable from the internet, or a new firewall forward.
- A push service learning the content: payloads are always encrypted (RFC 8291, `aes128gcm`).
- Enrollment that lets anyone on the LAN add a device without the owner creating the link.
- Pushes going silently nowhere: a dead subscription must surface as a failed delivery and a
  flagged device, like the Pushover device check.

## Constraints

- **Secure context.** Service workers and the Push API need HTTPS. The enrollment page,
  service worker script and report endpoints must therefore be served from the HTTPS external
  URL (`server.external_url`, e.g. `https://notify.example.org:8931`), not the plain-HTTP LAN
  address. If no HTTPS external URL is configured, creating an enrollment link is refused with
  a clear reason.
- **Origin is fixed.** A subscription belongs to the origin of the page that made it. Changing
  the HTTPS hostname or port later means re-enrolling the browsers; the admin UI should say so.
- **Browser must run.** Chrome/Edge show pushes only while the browser runs (background mode
  counts); Firefox must be open. A closed browser gets queued pushes when it starts, within TTL.
  The enrollment page should mention this (and Chrome/Edge's "continue running background apps").
- **Container limits.** The image is read-only, uid 1026, 320 MiB, 0.5 CPU. Prefer implementing
  VAPID (ES256 JWT) and RFC 8291 encryption with the already-present `cryptography` package and
  the shared `httpx` client, rather than adding `pywebpush` (which pulls in `requests` and
  is synchronous). This is a preference; the builder may choose otherwise with a reason.
- **Secrets.** The VAPID private key is generated on first use and stored encrypted like other
  provider secrets (never returned by the admin API). Subscription `auth`/`p256dh` keys are
  address secrets (`secret_address_fields`), encrypted at rest.
- **Provider etiquette.** Honour `429` + `Retry-After` (retryable), treat `413` as permanent
  (payload too large: shorten, then gap), `404/410` as subscription gone, `5xx` as retryable.
  The `Urgency` header follows severity (`very-low` … `high`).
- **Tracked files stay anonymous.** Examples use `alex` / `notify.example.org`; real host names
  go only into the server documentation.

## How it fits (decided)

- Provider adapter `notify/providers/webpush.py` behind the existing `Provider` contract:
  `send`, `plan`, `capabilities`, `config_problems`, `device_problems`. Address fields:
  `endpoint`, `p256dh`, `auth` (the last two secret), plus `user_agent` for display.
- Reports come in on an endpoint keyed by an unguessable per-delivery token included in the
  encrypted payload (no bearer token in the service worker). They feed the existing
  `apply_receipt` path, so `delivered`/`acknowledged` behave exactly as Pushover receipts do,
  events and webhooks included. Reports for unknown, finished or foreign deliveries are ignored.
- **Host guard (builder's call, confirmed with owner context):** the no-login browser paths
  (enrollment page, service worker, report endpoint) answer only when the request's host and
  port match `server.external_url`; elsewhere they are 404. The deployment has no public route
  (no `public_base_url`), but this keeps the paths off any route added later.
- Enrollment links reuse the single-use, hashed-code pattern of the access-request decision
  links (`/admin/decide/...`): code stored hashed, cleared on use, expired after 24 h.
- Dead-subscription handling reuses `device_check` (0.9.0) for the flag and the one-time alert.

## Left to the builder

- Exact URLs of the enrollment page, service worker and report endpoint.
- The payload format between hnl-notify and the service worker (it is our own; keep it small,
  under the ~4 KiB push limit after encryption, truncating the message with a marker).
- Default device naming from the user agent, and how the admin edits it.
- Whether the browser re-subscribes by itself when its subscription changes
  (`pushsubscriptionchange`) and updates the device; recommended where supported.
- Admin UI placement (a webpush provider card with *Create enrollment link*, and browser
  devices in Devices).

## Deployment notes (host side, outside the repo)

- A LAN-only reverse-proxy route with its own certificate on a port that is **not** forwarded
  by the firewall, pointing at hnl-notify's HTTP port. `server.external_url` /
  `NOTIFY_EXTERNAL_URL` changes to that HTTPS URL (links in Pushover and email follow).
- hnl-notify needs outbound HTTPS to the push services (it already has outbound internet for
  Pushover).
