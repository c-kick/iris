# Files and pasted images in group chats (design)

Status: final draft by Linden, 2026-11-10, for review by Quail (lead) and Oriole, then the owner's approval.
Roadmap items #1 (files in a chat) and #2 (pasted images): one design, one storage. Reviewed against the code by
Oriole and Quail; builder check run. "Owner" = the owner's decision; "call" = the designer's decision, open to review.

## Intent

Let the owner and agents share files (screenshots, logs, patches, PDFs) inside an Iris group chat, so a problem can be
shown instead of described. Success: the owner pastes an iPhone or desktop screenshot into the composer, an agent looks at
it and answers; an agent posts a log or a screenshot of its result and the owner sees it in the thread.

Anti-goals: an agent's context flooded with megabytes it cannot use; a file that becomes a way to run script on the
admin page; a database that grows without bound; any content shift in the thread or composer.

## Owner decisions

1. Who posts: the owner (admin composer) and agents (MCP `group_post`).
2. Agents read files on demand: messages list their files; a new tool `group_file` returns one.
3. Any file type is accepted. Raster images also render as a preview in the thread; everything else as a chip.
4. Retention: the existing `attachment_retention_days`; afterwards the bytes are purged and the message shows
   "file expired".
5. Per message: at most 10 files, 10 MiB in total (decoded), as for notifications.
6. Agents upload over MCP as inline base64, up to 5 MiB decoded per chat post; `send_notification` keeps 256 KiB.
7. Composer: paste image, attach button (iPhone: Photos, Files, Camera), drag and drop.
8. A message may hold only files and no text; it is still a request, FYI or @mention for claims and wake-ups.
9. Per chat at most 100 MiB of unexpired files. A post that would go over it does not fail: the oldest files of that
   chat are expired first (bytes purged, "file expired") until it fits. (Call: automatic, oldest first, and the
   thread shows one line "Older files expired to make room (N files)". This is the designer's reading of "allow
   culling, so a chat can continue"; a manual button instead is the alternative.)
10. Big images handed to an agent are shrunk by Iris (Pillow becomes a dependency).
11. Files that are neither image nor text are not returned into an agent's context: a `notify-chat file ID` command
    saves them on the agent's host and prints the path.

## Measured

`scripts/memprobe.py` under a 320M cap (2026-11-10): one 13.3 MiB JSON body with 10 MiB of attachments peaks at
185 MiB RSS, three at once at 271 MiB. A 5 MiB MCP post is a ~7 MB body and at most two large bodies are read at
once, so 5 MiB holds. Re-measure once with a real MCP `group_post` before release (the MCP SDK parses separately).

## Storage

- Table `chat_files(id TEXT PRIMARY KEY, message_id TEXT REFERENCES chat_messages(id) ON DELETE CASCADE (NULL while pending),
  group_id TEXT NOT NULL, idx INTEGER NOT NULL, filename, content_type, size, sha256, kind TEXT NOT NULL
  ('image'|'text'|'other'), width, height (images), data BLOB, purged_at TEXT, created_at TEXT NOT NULL,
  UNIQUE(message_id, idx))`. Call: one opaque id per file (`cf_…`, like other ids) used by both the admin route and
  `group_file`; `group_id` stored for the per-chat cap and the access check.
- Owner uploads before posting: files exist with `message_id NULL` ("pending") until the post names them; pending
  files older than 1 hour are deleted by the sweep. (Call; follows from the upload route below.) Pending files count
  towards neither the cap nor the message, but are bounded per group: at most 10 pending files / 10 MiB (Quail).
  Pending files hang on `group_id`.
- `kind` is decided by content, not the declared type: `image` only when the magic bytes are PNG, JPEG, GIF or WebP
  (SVG is never an image here); `text` when the declared or guessed type is `text/*`, json, xml, yaml, csv, x-patch,
  x-diff, or the type is unknown, and the bytes decode as UTF-8 without NULs; everything else `other`. (Call.)
- Names and types cleaned with `attachments.clean_filename` / `clean_content_type`.
- Schema version 5; additive migration; `docs/upgrading.md` notes that backups grow with the files.

### Deletion

- Retention: in `core.retention_sweep`, purge `chat_files` whose `created_at` is older than
  `attachment_retention_days` (`data=NULL, purged_at`).
- Cap: on each post, within the post's transaction, expire oldest unexpired files of the group until
  `sum(size) + new <= 100 MiB`.
- Every path that deletes `chat_messages` (`_purge_groups`, the closed-group sweep, the ageing of old messages in open
  groups) also deletes their files: the FK cascade covers it, and `_purge_groups` gets an explicit line in its style.
  Each path gets a test.

## Owner upload (admin)

- `PUT /admin/api/chats/{group_id}/files?name=…` with the raw bytes (`Content-Type` = the file's type, or
  `application/octet-stream`), one request per file; returns `{id, name, type, size, kind, width, height}`. No
  multipart, so no new dependency and no CSRF weakening: the route requires a session and the `Origin` check that the
  JSON routes get from the preflight (`_check_browser_mutation` extended to non-JSON bodies on this route).
- `BodyLimit` learns this one route: a valid admin session cookie counts as authenticated (checked before the body is
  read), route cap 10 MiB, inside the existing large-body slots.
- `POST /admin/api/chats/{group_id}/messages` gains `files: [id, …]` (pending files of this group, uploaded in this
  session); text may then be empty. The 10 files / 10 MiB rule is checked here.
- `GET /admin/api/chats/files/{id}`: session only (a bearer admin never sees chat content, as for message text).
  `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`. `kind='image'` is served
  inline with its sniffed type; everything else as `Content-Disposition: attachment`. Expired: 410.
  `?thumb=1` is not needed: the page shows the original, browser-scaled and lazy-loaded (call; Quail agrees).

## Agents (MCP)

- `group_post(…, files=[{filename, content_type, data_b64}])`: up to 10 files, 5 MiB decoded in total. `BodyLimit`
  on `/mcp`: bodies above 1 MiB need the `chat` scope (via `large_scopes`), route cap ~7.5 MiB; each tool still
  enforces its own limit, so `send_notification` keeps 256 KiB.
- `group_file(member_handle, file_id, offset=0, limit=…)`:
  - Access: the file's message must be one this member can read through `group_read` (its own group, posted after it
    joined). Files listed in an owner-approved `group_history` are fetchable only for the member that history was
    approved for, while that approval is valid. Anything else: "not found". (Call: same gate as message text.)
  - `image`: MCP image content. Over 1568 px on the long side or 3.75 MB, Iris scales it down with Pillow (PNG stays
    PNG, others become JPEG q85), and says so in a text part ("scaled from 4032x3024"). Width/height are read from the
    header only, at upload. Above 40 megapixels (Pillow's own decompression-bomb guard stays on) the image is not
    decoded and is answered like `other`: "too large to show, save it with notify-chat file <id>". Scaling runs in a
    worker thread; animated GIF/WebP use the first frame only. (Quail: a 10 MiB JPEG can decode to hundreds of MB.)
  - `text`: text content, at most 100 KB per call, cut on a line boundary, with "bytes A–B of N; call again with
    offset=B" when there is more.
  - `other`: no bytes; name, type, size and the line "save it with: notify-chat file <id>".
  - Expired: "file expired on <date>".
  - All returned text is marked untrusted, like message text (filenames and file contents are the sender's data).
- `notify-chat file <id> [--dir DIR]`: fetches over REST with the session's stored member handle
  (`GET /v1/chat/files/{id}`, same access rule; the member handle goes in a header, never in the URL), saves to `DIR` (default `~/.cache/hnl-notify-chat/files/`, 0700) with
  a cleaned name, prints the path. Hosts need the updated script (a host with its own copy needs it copied again).

## In messages and frames

- `frame()`, `history_frame()` and the wake text list the files inside the quoted, untrusted body, after the text:
  `| [file cf_… · screenshot.png · image 1170x2532 · 812 KB]`, names cleaned and marker-stripped like body text, so a
  filename can never look like a header. `group_read` JSON also gets `files: [{id, name, type, size, kind, expired}]`
  per message. The relay passes the server's frame through, so Codex needs no relay change for the listing.
- File-only messages: `text` stored as `''`. `_post` accepts empty text when files are present. Every reader falls
  back to "2 files: a.png, b.log": `frame`/`_quote_body`, `intent_text` (a file-only first post sets that line as the
  intent), `reply_excerpt` (a quote-reply to a file-only message quotes that line), the list preview in
  `admin._group_out`, push previews, and `chatPlain`/`chatMarkdown` in `app.js`.
- Bell and notices are untouched (`kind='alert'` rows never carry files).

## Composer and thread (admin UI)

- Paperclip button in `.compose-side` (opens the system picker: `<input type=file multiple>`); paste of image data
  into the textarea; drop onto the thread or the composer (a drop overlay that does not move anything).
- Pending files show as a strip of chips/thumbnails with a remove (x) and an upload progress state, overlaying above
  the composer; the strip never pushes the thread (no content shift). Send is disabled while an upload runs.
- Errors (too large, cap, upload failed) show in the composer's existing error line.
- In the thread: images as previews (max ~280 px high, reserved size from `width`/`height` so loading never shifts
  the thread), tap/click opens the original in a new tab; other files as chips (icon, name, size, download).
  Expired files: a muted chip "file expired".

## Not in scope

Editing or deleting single files by hand; files in notifications' email replies (they already exist); thumbnails
made by the server; virus scanning.

## Tests the build needs (minimum)

Upload limits and the session-aware body limit; Origin check on PUT; magic-byte kind (an HTML file named x.png is
`other` and downloads); access rules for `group_file` and the REST fetch; image scaling; text paging; file-only
messages through every reader listed above; frame quoting of hostile filenames; cap expiry order; retention and every
deletion path; MCP 5 MiB accepted and 256 KiB still enforced for `send_notification`; the 40-megapixel refusal,
pending-file bounds; memprobe re-run with a 5 MiB post and with a large image being scaled.

## Also in the build

Pillow pinned in `requirements.txt` and the image rebuilt; MCP server instructions and tool docstrings;
`docs/group-chats.md` (the Planned section goes), `docs/api.md`, `docs/architecture.md`; version 0.20.0.

## Build split (Quail, lead)

A (Quail) storage core on branch `chat-files`: schema 5, `notify/chat_files.py`, `_post` with files, deletion paths.
B (Oriole) what agents see: frames, `group_read`, readers of `text`, MCP `group_post(files)` / `group_file`, `/mcp`
body limit, REST fetch, `notify-chat file`. C (Linden) what the owner sees: upload/download routes, session-aware
`BodyLimit` and Origin check, composer and thread UI. B and C branch from A; Quail merges, full suite, fresh review;
deploy only on the owner's OK, DB backup first.
