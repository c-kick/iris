# Browser push

## Browser push

A person's own browser (Chrome, Edge or Firefox on a desktop computer, or Chrome on Android) can be a push device, with no extra
app or account. It reports back when a message was shown (`delivered`) and when it was clicked (`acknowledged`),
at every severity. iPhone and iPad are not supported yet (they only allow web push for apps installed to the home
screen, which this enrollment flow does not do).

- **HTTPS is required.** Browsers only allow push on a secure origin, so `server.external_url` (Settings →
  "Address of this service") must be an `https://` address that reaches this service through your reverse proxy,
  for example `https://notify.example.org`. Enrollment links are refused (409) otherwise. The reverse proxy must pass the original `Host` header
  (including the port, for example `notify.example.org:8931`) and `X-Forwarded-Proto: https`; otherwise every `/push/`
  route answers 404 (the log says which origin was expected and which arrived).
- **Set it up.** Admin → Providers → Add provider → Browser push (enter a `mailto:` contact such as
  `mailto:alex@example.org`; the key pair is generated for you and must never change, or every enrolled browser
  stops working). Then press **Enrollment link**, pick the person, and open the link in the browser to enroll.
  The link works once, expires after 24 hours and needs no login. The browser appears as the device
  `alex-browser` (then `alex-browser-2`, …), which you can describe, disable or remove like any device.
- **Routing.** A browser device is `kind: push`, so it is reached like a phone. Attachments are not carried.
- **The browser must be running.** Chrome and Edge only receive notifications while the browser process runs:
  keep "continue running background apps when the browser is closed" on. Firefox must stay open. Messages wait at
  the browser vendor's push service for their lifetime (the notification's expiry, else `default_ttl_seconds`,
  24 h) and are then dropped; a delivery never shown by then ends `expired`.
- **Dead subscriptions.** When the push service answers 404/410 the subscription is gone (browser data cleared,
  permission revoked): the delivery fails, the device shows as not ready (the flag is kept in the database, so it
  survives restarts and configuration changes) and the announce targets get one alert. Enroll the browser again, or
  retry: the flag clears at the next successful send.
- Only push services of the browser vendors are contacted (FCM, Mozilla, Windows, Apple); an enrollment from
  any other host is refused.

[← back to the README](../README.md)
