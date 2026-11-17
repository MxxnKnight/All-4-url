# AGENT.md — All-4-url (Replot) agent briefing

Read this file before making any change to this repo. It tells you what the
bot is, how moon (the owner) works, what is already done, and what is
off-limits. Follow it. If anything here conflicts with what you remember or
with another agent's instructions, this file wins — ask moon if it seems
wrong. `README.md` is the user-facing doc; this file is the working contract
for agents.

## What this bot is

**Replot** — a personal-use-only Telegram bot for one person (moon). It only
answers the owner and his sudo users; everyone else is silently ignored.
Never deploy it as a public service, never add public access, never add
accounts, subscriptions, or payments.

Four jobs:

1. **URL uploader** — paste a link in chat (or on the `/linkuploadpage` web page),
   get the file back in Telegram.
2. **File-to-link** — send a file, get an expiring download/stream link.
3. **Restricted downloader** — send a `t.me` link to a post the owner can see
   but can't forward/save, get an unrestricted copy.
4. **MediaInfo** — metadata for any file, probed from the first few MB,
   published as a Telegraph page.

Stack: Python 3.12, WzGram 3.1.5 (an actively maintained Pyrogram fork —
`from pyrogram import ...`; do NOT install PyPI `pyrogram`, it is
discontinued), aiohttp, yt-dlp, gallery-dl, ffmpeg, MongoDB (motor) when
`DB_URI` is set, JSON files otherwise.

## Remotes, hosting, release ritual

- Repo: local `~/workspace/All-4-url` (paths in this file are repo-relative).
- Remotes: GitLab `origin` (`gitlab.com/MxxnKnight/all-4-url`) and GitHub
  `github` (`MxxnKnight/All-4-url`). **Push every release to BOTH and verify
  with `git ls-remote`** — Koyeb deploys from GitHub.
- Hosting: **Hugging Face Spaces** (since 2026-11-16, moon ordered the
  move and accepted the account risk) — grandfathered free Docker Space
  `Adak2b5/Strom`, app `https://adak2b5-strom.hf.space`, 2 vCPU, 16 GB
  RAM, 50 GB ephemeral disk, no published egress cap; sleeps after
  inactivity, a cron-job.org `/health` pinger keeps it awake. Deploy by
  pushing the repo to the Space (see `HF_DEPLOY.md`); the Space rebuilds
  on every push. Koyeb free is the stopped fallback only (512 MB RAM,
  0.1 vCPU, 2 GB disk, 100 GB/month outbound) — never run both at once,
  two instances polling one bot token fight over updates.
- Telegram cap: 2 GB per file (`MAX_FILE_SIZE` = 2,000,000,000).
- Release ritual: bump `BOT_VERSION` in `config.py` → test (see Testing) →
  descriptive commit → push both remotes → verify with `git ls-remote` →
  deploy to the HF Space (push the repo files there; the Space rebuilds).
  Docs-only commits do NOT bump the version. On startup the bot sends a
  "Bot updated to vX.Y.Z" notice to the owner + sudo users once per version
  (`core/version_notify.py`) — so a version bump is also a user notification.
- **Current version: v1.38.0** (2026-11-17).

## How moon works — the contract

- **Plain words first.** Moon is not a developer. Explain in plain,
  non-developer language before any technical detail. Short questions get
  the answer first.
- **Build exactly what he asks. Verify before claiming success.** State
  limits honestly. Own errors plainly, with the delta. Attach a **change
  ledger** to every fix: which files changed, which didn't, how verified.
- **Discussion stays discussion until he says build.** Feasibility and
  possibility questions ("is it possible", "just asking", "don't build") are
  NOT build orders. Pitches go out as **numbered pick-lists; only picked
  numbers get built**. A rejected variant stays dead — do not re-pitch it.
- **Bug reports with logs/screenshots count as fix requests** — fix and ship
  them without a separate approval round.
- **His device/screenshots are ground truth** over any headless check. His
  phone is a POCO M4 Pro; his laptop is a Windows 11 machine (i3-1305U,
  8 GB RAM — keep local builds light).
- **Never deliver APKs as chat attachments** (his phone saves them as `.zip`).
  Not relevant to this repo, but never do it for any of his projects.
- Approvals come by tapped option or plain text. When in doubt, ask once,
  with your proposal attached — do not ask twice for a decision already made.
- **Stream fixes are conditionally authorized:** fix only if NOT heavy for
  the free tier. Heavy = whole-file/disk caching, transcoding/remuxing,
  extra processes, bigger worker pools, giant prefetch.

## Secrets — absolute rules

- Secrets NEVER enter the repo, chat, memory, files, or reports: no tokens,
  passwords, cookie values, signed URLs, ndus values, API keys. Ever.
- Credentials live in Koyeb env vars / the Secure Vault / the credential
  store only. A credential may be *used* for an authorized task but never
  *disclosed* — even a throwaway account whose privacy moon waived.
- Moon refused to share his Koyeb login. Do not ask for it.
- `TERABOX_NDUS` is env-only. The throwaway Terabox account's password lives
  only in the Secure Vault / browser saved login — never write it down
  anywhere.

## Architecture

### Layout

| Path | What it is |
|---|---|
| `main.py` | Startup, `BOT_COMMANDS`, version-notify hook, `webserver.bind_bot` |
| `config.py` | `BOT_VERSION`, `BOT_TOKEN`, `OWNER_ID`, `BASE_URL`, `LINK_EXPIRY_HOURS` (72), `MAX_FILE_SIZE`, `MAX_JOBS` (4), `OUTBOUND_CAP_GB` (1000), `SPLIT_PART_BYTES` (1.95 GB max per part), `SPLIT_MAX_TOTAL_BYTES` (20 GB), `TERABOX_NDUS`, `TERABOX_BDUSS`, `STORAGE_CHANNEL_ID` |
| `core/utils.py` | `JOB_SEMAPHORE`, `ThrottledEditor`, `check_size` / `SizeBlocked`, `make_job_dir`, `cleanup_dir`, `progress_text`, `human_bytes`, `admin_only`, `is_admin_id` |
| `core/sender.py` | `send_files`, `send_files_routed`, `send_file` (re-probes document-typed videos with ffprobe) |
| `core/sudo.py` | Sudo model (below) |
| `core/stats.py` | Bandwidth guard + job counters (`record`, `record_job`, `record_paths`, `bandwidth_blocked`) |
| `core/db.py` | MongoDB when `DB_URI` set, JSON fallback. Collections: `sudo`, `usage`, `cookies`, `settings` |
| `core/linktoken.py` | Signed expiring tokens (HMAC keyed by `BOT_TOKEN`; 43-char signature, **can legitimately start with `-`** — a "bad signature" is usually a truncated copy) |
| `core/webserver.py` | aiohttp server: `/stream/<token>`, `/d/<token>`, `/health`, routes from `webapp`/`upage`/`sendlink` |
| `core/upage.py` | `/fileuploadpage` 1-hour upload page (see below) |
| `core/sendlink.py` | `/linkuploadpage` permanent link-upload page (see below) |
| `core/pipe.py` | `/pipe` Telegram-direct chunked pipe |
| `core/unzip.py` | One-by-one zip extraction (`zip_info`, `iter_extracted`; caps: zip ≤2 GB, total ≤20 GB, ≤1000 files) |
| `core/filemenu.py` | File-menu builders (`fm:` callbacks) |
| `core/videotools.py` | Screenshots / sample clips (ffprobe/ffmpeg) |
| `core/mediainfo.py` | Partial probe (head chunks + MP4/MOV moov tail, ~16 MB max) |
| `core/extractors/` | `direct.py`, `ytdlp_engine.py`, `gallery_engine.py`, `scrape.py`, `terabox.py`, `filesnag.py`, `site_registry.py`, `player_scan.py` (shared browser-free helpers), `streamtape.py`, `vidara.py`, `filehosts.py` (gate walker), `luluvido.py` |
| `plugins/` | Pyrogram handlers. **NOT a package** (no `__init__.py`, `plugins=dict(root="plugins")`) — no plugin-to-plugin imports; shared logic lives in `core/` |

### Sudo model

`sudo.is_admin` = owner OR env `SUDO_USERS` OR runtime `/addsudo` ids
(`sudo.list_sudo()` returns a **tuple of two sets** — sum both). Only the
OWNER can add/remove sudo. `admin_only` = owner+sudo; `owner_only` =
OWNER_ID only; non-matching users get total silence.

### Destination model

Destination is **bot-wide per feature, not per account**
(`settings.get_dest(feature)`); `/destination` is owner-only.
`sender.send_files_routed(bot, chat, paths, feature, ...)` routes by
feature; `"bot"` = requester's chat, `"saved"` = user client to its own
Saved Messages (needs `SESSION_STRING`, falls back to bot).
`sender.send_files` sends directly with no routing — used where bot-PM-to-
owner delivery is explicitly required (upage/sendlink admin delivery).

### Bandwidth guard

Monthly outbound = uploaded + served only. Owner alert at 80% of
`OUTBOUND_CAP_GB`; new download jobs pause at 95%
(`stats.bandwidth_blocked()`, `stats.bandwidth_pause_text()`); streaming is
never blocked. `stats.record_paths("uploaded", paths)` inside
`sender.send_files` records upload bytes automatically.

### Storage = Telegram itself

File-to-link storage is the owner's bot PM message by default
(`STORAGE_CHANNEL_ID` is only an optional permanence override that was never
configured). Signed expiring tokens via `core/linktoken.py`.
`/stream/<token>` + `/d/<token>` relay Telegram → Koyeb → player via
`stream_media` (Range-capable, per-chunk fetch since v1.18.2). Deleting the
original PM message breaks its file-to-link links.

### The URL resolver chain — keep this order

`plugins/uploader.py::_dispatch`, in order:

0. Own-domain `/u/` upage links → `_upage_local_flow`
1. Terabox (`terabox.is_terabox_url`) → ndus cookie route first, FileSnag
   fallback (`core/extractors/filesnag.py`: fresh nonce every call from
   `FSN_DL` settings, ~12 h rotation, never hardcode)
1b. Custom sites (`site_registry.match(url)` → `_custom_flow`)
2. Instagram photo posts (`"instagram.com/p/" in url`) → `_instagram_carousel`
   (yt-dlp metadata → `carousel_image_urls` → download each image) then
   `_gallery_flow`
3. Direct (`check_direct(url)` → `_direct_flow`)
4. yt-dlp (`_ytdlp_flow` — picker when ≥2 heights)
5. gallery-dl (`gallery_engine.download`)
6. Scrape (`_scrape_flow`: `scrape.find_media_url`; playlist → yt-dlp;
   else direct download with `Referer` header)

All bot flows end with `_maybe_unzip_prompt`, then `send_files_routed(...,
"uploader", ...)`, and `cleanup_dir(job_dir)` in `finally`. `_dispatch`
returns `"done" | "pending" | "failed" | "blocked"`.

### Web pages — shared rules

- `/fileuploadpage` (`core/upage.py`, `plugins/upage.py`): **1-hour** page for
  uploading files to the server. Page token carries `uid`. Uploads in 16 MB
  chunks with auto-resume; each finished file gets a single-use signed
  download link (`/u/<token>`). Limits: 750 MB advertised per file, 1 GB
  hard ceiling, 1 GB total pending, 256 MB headroom. Page has "Upload to
  admin" (delivers to OWNER_ID bot PM, bypassing `/destination`) and Delete
  buttons. When any sudo sends a `/u/` link back to the bot, the file goes
  to the owner's bot PM with a preceding note — the sudo gets no copy.
- `/linkuploadpage` (`core/sendlink.py`, `plugins/sendlink.py`): **permanent**
  (no expiry), unlisted page for uploading by pasting a link. One tap:
  paste → Fetch → job card walks Probe → Check → Download → Upload →
  lands in the owner's bot PM. Best quality always, no picker. Textbox
  clears on Fetch. Max 5 jobs at once per page link (2 really downloading);
  a 6th submit is refused (429). Job history (the cards) deletes itself
  after 1 hour; the downloaded file is deleted the moment its upload
  finishes. Uses the **same** resolver chain as the chat uploader, in the
  same order, including images (direct image links, Instagram carousels,
  Reddit/Imgur via gallery-dl).
- Both pages are **fully self-contained**: no external scripts/fonts/images,
  no cookies, no IP/UA logging. Both send `X-Robots-Tag: noindex, nofollow`
  and are disallowed in `/robots.txt` (`/up/`, `/u/`, `/d/`, `/stream/`,
  `/sendlink/`). Preserve these properties in any UI work.

### Other features (current)

- `/pipe` (`core/pipe.py`, `plugins/pipe.py`): Telegram-direct chunked pipe,
  direct files only, ≤2 GB, sent as document; HLS/DASH via ffmpeg (VOD only,
  live refused). **No split, no mid-transfer cancel** (designs exist, moon's
  pick still open). Pipe is NOT used inside the normal uploaders — moon
  decided that explicitly ("since i want upload as videos, using pipe
  everywhere is bad idea!").
- `/vidsplit`: oversize **direct videos only**, time-split playable parts
  (≤1 GB/part by default, SEGMENT_MAX_MB option 50–1900, total ≤8 GB, one part on disk).
- `/imghost`: reply to a photo/image → Catbox upload, then freeimage.host as
  fallback (FREEIMAGE_KEY overrides its public key), temp deleted in `finally`.
- File menu (`core/filemenu.py`, `plugins/filemenu.py`): sending a file
  replies with a menu — Generate links, MediaInfo, Convert, Screenshots,
  Sample, Unzip, Close. Menu edits use the `_edit_menu` helper so a failed
  edit shows the real Telegram error as an alert instead of dying silently.
- `/convert`: re-sends the same `file_id` as video/document — no download,
  no Koyeb outbound.
- Unzip: one-by-one extraction, upload each file, delete, next. 1 s pacing
  between sends (FloodWait protection).
- `/status`, Mini App panel at `/app` (with `#tgWebAppData` fallback).
- Update notification on startup per version (see Release ritual).

## Hard lessons — do not relearn these

- **Always `node --check` the extracted `<script>` of any page change
  before shipping.** A literal newline inside a JS double-quoted string
  once killed an entire page's script (v1.25.1 root cause: pick/drop dead
  on all platforms).
- **Silent HTTP access log:** aiohttp 3.14 needs `_SilentAccessLog` on
  `web.AppRunner`; bare instantiation crashes startup. Always test the real
  `start_web_server()` path, not just `make_app()`.
- **Callback data is limited to 64 bytes.** Menu callbacks carry
  `<chat>:<msg>` only, with short prefixes (`fm:`/`cv:`/`fs:`/`fp:`/`mi:`).
- **Plugins are NOT a package** — no cross-plugin imports; shared logic in
  `core/`.
- **`sudo.list_sudo()` returns a tuple of two sets.**
- **A truncated link token looks like a "bad signature".** Always copy via
  the Stream button / address bar; signatures can start with `-`.
- **Terabox `ndus` is env-only** — `/setcookies` and cookie jars do NOT
  activate it. 1DM content links extracted on moon's phone are IP/session-
  locked — page links work for the bot, extracted content links don't.
- **FileSnag resolve is slow/variable (~1 s to ~31 s)** and downloads can
  crawl (~50 KB/s observed once). That is the fallback, not a bug.
- **Never write `/*` inside a Kotlin comment** (nests, unclosed comment) —
  not this repo's language, but the rule travels: check comment nesting
  when touching any language with nested block comments.
- **No backticks in `git commit -m "..."`** — bash executes them and the
  words vanish from the message. Use plain words or `git commit -F`.
- **Koyeb has no documented metrics/egress endpoint** — the bot's own
  bandwidth counter is the source of truth.
- **Stream seeks:** per-chunk fetch only (`_stream_window`, one chunk per
  `stream_media` call, 20 s timeout, 4 attempts). Long seeks in mpvRx are
  the ground-truth test; the Download link played locally is the fallback.

## Testing

- `python -m py_compile` every touched module.
- Web pages: extract `PAGE_HTML`'s `<script>` and run `node --check` on it;
  assert the page has no external refs and the paste box is above the
  explanation.
- End-to-end: run `make_app()` on a test port with a fake bot client
  (records `send_video`/`send_document`/`send_message`, stubs `get_chat`)
  plus a tiny local HTTP server serving a small file — submit → poll status/
  jobs → done, fake bot received the file, job dir cleaned. Bad token → 410,
  bad URL → 400.
- Do not run internet probes that depend on proxy credentials; local
  aiohttp servers are unaffected and are the reliable test surface.
- Check `git status` before committing; never commit secrets, `data/`
  runtime files, or `__pycache__`.

## Current status (2026-11-17) — v1.38.0

- v1.38.0 adds multi-link messages (moon's order): paste several
  links in one message and they download one after another, each
  with its own status message ("Working on it… (1 of 3)").
  `_extract_urls` (new) returns every uploadable URL in order,
  de-duplicated, skipping t.me links (the restricted plugin owns
  those); `_extract_url` is now its first-result wrapper, so
  /browser and /gallery are unchanged. Sequential by design (each
  link is a full download + upload job; parallel runs would fight
  over the 2 CPUs and the job slots). A failed link never stops
  the rest; a "pending" link resolves through its own button
  callback while the loop moves on; "blocked" (bandwidth cap)
  stops the loop. Cap: MAX_MULTI_LINKS = 10 per message, the rest
  skipped with a note. Single-link messages behave exactly as
  before (same status text, same reactions). Verified: 23 checks
  in /tmp/test_v138.py.
- v1.37.1 shortens the /start message with a FREE collapsible
  blockquote (Bot API 7.4, no Premium needed; older clients just
  show a normal quote). HOME_TEXT keeps its short intro visible
  and wraps the whole feature list in one expandable blockquote
  ("Full feature list — tap to expand"), written in Pyrogram's
  markdown quote syntax: first line `**>`, every following line
  (incl. blank ones) prefixed `>`, last line ending `||` (the
  parser strips it — it is a terminator, not a spoiler). Verified
  by parsing HOME_TEXT with Pyrogram's own Markdown parser:
  exactly one blockquote entity with collapsed=True, covering the
  tail to the message end, bold/code intact inside, no spoiler
  entity, no stray markers. Also fixed the stale unzip caps in the
  same text (1 GB / 8 GB / 200 files → 2 GB / 20 GB / 1000 files,
  the v1.34.0 limits). 27 checks in /tmp/test_v1371.py. Note for
  future parser tests: this Pyrogram's Markdown.parse returns a
  dict {"message", "entities"} of RAW types, and the raw
  MessageEntityBlockquote flag is `collapsed`, not `collapsible`.
- v1.37.0 fixes the thumbnail-instead-of-video bug AND adds the two
  override commands moon asked for. Root cause, proven on a real
  StreamFile page (replayed its saved HTML): the static scrape's
  PATTERNS matched `og:image` BEFORE any video pattern, so the
  placeholder page's thumbnail was downloaded and sent, and the
  browser scan (incl. v1.35.0 click-to-play) never ran. gallery-dl
  was NOT the culprit — it answers "Unsupported URL" for
  streamfiles.net (verified with gallery-dl 1.32.20); an earlier
  explanation blaming gallery-dl was wrong. Fixes: (1) og:image
  moved LAST in scrape.py's PATTERNS, so a real video URL in the
  same HTML always wins; (2) a new `scrape.is_image_url` helper,
  and both scrape flows (chat uploader `_scrape_flow`, sendlink
  `_download_resolved` gallery branch) now give the browser a turn
  when the static result is only an image — video wins if found,
  the thumbnail is the fallback when the browser finds nothing.
  (3) New commands `/browser` (force_browser flag on
  `_scrape_flow`: skips the static steps entirely) and `/gallery`
  (runs `_gallery_flow` only). Menu is now 22 commands; both new
  commands are in the uploader's exclusion list (the v1.36.1
  lesson). Verified: 45 checks in /tmp/test_v137.py (pattern order,
  real-page replay, both flows mocked incl. fallbacks, command
  routing, menu + exclusion). Live proof pending: moon re-sending
  the StreamFile link — this also proves v1.35.0 click-to-play,
  which has never run on a real page yet.
- v1.36.1 fixes a pre-existing double-fire found while wiring
  /live: the uploader's URL handler excluded only start/help/batch/
  cancel/mediainfo/live from its command filter, so `/pipe <link>`
  and `/vidsplit <link>` ALSO ran the normal uploader chain on the
  same link (a second download of the same thing, double bandwidth
  and double sends). Both are now in the exclusion list. Any future
  command that takes a URL argument MUST be added to that list in
  plugins/uploader.py (the exclusion is a manual list, not
  automatic).
- v1.36.0 adds `/live` (moon's order): record a live stream into
  Telegram, part by part. New files `core/live.py` + `plugins/live.py`.
  yt-dlp probes the link (refuses non-live / not-started / ended
  streams in plain words) and hands ffmpeg the best stream URL
  (combined format preferred, else best video-only + best audio-only
  as two inputs; Referer + User-Agent forwarded, cookies never).
  ffmpeg records with stream-copy into the segment muxer
  (`-segment_time` = LIVE_SEGMENT_MIN minutes, default 15,
  env-overridable 5–60; `-reset_timestamps 1`; mp4 parts) — the
  muxer cuts by TIME only, there is no byte-size option (verified
  against ffmpeg's own muxer help), so the part length is the only
  knob; at live bitrates a 15-minute part is a few hundred MB, far
  under the 2 GB cap. A watcher sends each finished part as a video
  the moment it closes (a part counts as complete ONLY when a newer
  part exists or ffmpeg has exited — never the part still being
  written), then deletes it from disk. A ⏹ Stop button
  (`lv:stop:<8-hex-job-id>`, 16 bytes) ends it cleanly (SIGTERM,
  SIGKILL only if ffmpeg refuses); a stream ending on its own, or
  the bandwidth cap, ends it the same way. ONE recording at a time
  (own LIVE_LOCK — deliberately NOT JOB_SEMAPHORE, a hours-long
  recording must not hold a download slot); a second /live gets an
  "already recording" reply. A Space restart kills a recording with
  no resume — accepted, not fixed. No streamlink, no third-party
  plugins: moon confirmed skipping the unofficial Stripchat
  streamlink plugin (yt-dlp already reads Stripchat directly).
  Wiring: menu is 20 commands now; `/live` added to the uploader's
  command-exclusion list so the URL handler doesn't double-fire on
  it. Verified: 48 checks in /tmp/test_v136.py (mocked yt-dlp +
  ffmpeg + sender — the sandbox cannot reach live sites); the live
  proof is moon sending a live link to the bot.
- v1.35.0 teaches the headless browser to click play (moon's order).
  Some players show only a thumbnail + play button and request
  NOTHING until a click — StreamFile's own code says so ("a page
  that is only looked at — or loaded by a bot — adds no view"), which
  is why moon's streamfiles.net link failed at every engine (the
  registry also misses streamfiles.net — only streamfile.net is in
  the host table — but the alias alone would NOT fix it: the walker
  does static form/media scanning and this page has no download form
  and no media URL in its HTML, so the alias was deliberately not
  added). When the first load finds no media, the browser now tries
  up to 3 clicks on a narrow play-only selector list (StreamFile's
  #vplayer button first, then Plyr/Video.js/generic play controls),
  waits for a new media response after each click (8 s max, ends
  early), and re-checks network + DOM + rendered HTML. Popups get
  closed; a click that navigates away is undone with go_back; a
  captcha revealed by a click still fails honestly. The no-cache
  alternative (fetch the page with ?nc=1 — verified 2026-11-17: the
  uncached page carries the real .m3u8 and it serves 200) was NOT
  built — moon picked the click approach instead. Verified: 14
  checks in /tmp/test_v135.py against a mocked Playwright (no real
  Chromium in the sandbox); the live proof is moon sending the
  StreamFile link to the bot.
- v1.34.0 is moon's picks from the "bigger hosting" list (1, 2, 4-partial,
  5; 3 download cache NOT picked, do not build unprompted):
  (1) zip caps raised — zip ≤2 GB (the real ceiling: Telegram's own
  cap for sent files, and link zips over 2 GB are split before they
  reach the unzipper, so a higher number would be dead config),
  total ≤20 GB, ≤1000 files (was 1 GB / 8 GB / 200);
  (2) MAX_JOBS default 2 → 4 (downloads are I/O-bound; FloodWait
  retry, 1 s pacing, BIG_FILE_LOCK and BROWSER_LOCK all still
  apply) — NOTE: the Space variable MAX_JOBS=2 overrides the code
  default until moon changes it to 4 in Space Settings;
  (4) /vidsplit total cap NOT raised (moon's call) but the part
  size is now a documented option — SEGMENT_MAX_MB was already
  env-overridable, now clamped 50–1900 so a part can never exceed
  Telegram's 2 GB cap; (5) parallel downloads for EVERY direct
  link ≥20 MB (download_file now routes through
  download_file_parallel, which falls back to single-stream on any
  doubt), with a PARALLEL_DOWNLOADS Space-variable toggle (1 = on,
  0 = off — honoured inside download_file_parallel, so Terabox /
  FileSnag's direct calls respect it too). Moon also decided the
  outbound guard stays at 100 GB (he does not believe HF has
  1000 GB of bandwidth) — .env.example + HF_DEPLOY.md now say 100;
  the code default stays 1000 as a runaway safety. aria2 /
  JDownloader were asked about and answered no: the parallel
  downloader covers aria2's speed win without a second process,
  and JDownloader is a heavy Java desktop app unsuited to a
  2-CPU Space. Verified: 26 checks in /tmp/test_v134.py (config
  defaults + env clamps, zip caps incl. 201-file pass / 1001-file
  refuse, parallel dispatch with mocks, byte-identical downloads
  against a local Range server incl. toggle-off and no-Range
  fallback).
- v1.33.0 renames four commands moon found confusing (his picks,
  no aliases — the old names stop working): `/sdl` →
  `/vidsplit`, `/upage` → `/fileuploadpage`, `/sendlink` →
  `/linkuploadpage`, `/phost` → `/imghost`. Handlers, menu
  (`BOT_COMMANDS`), help text, and every user-facing message
  updated. Deliberately UNCHANGED: the `/sendlink/{token}` web
  route (moon's permanent link-upload page link keeps working),
  module/file names (`plugins/sdl.py` etc.), and the internal
  stats key `"sdl"` (renaming it would split job history).
  Telegram caches the menu — moon may need to reopen the chat
  to see the new names. Verified: 25 checks in
  /tmp/test_v133.py (menu + handlers + no old command strings
  left + route intact).
- v1.32.0 adapts six picks from the WZML-X review (moon picked
  1, 2, 3, 5, 6, 7 of 8; 4 filename-cleaner/TMDB posters and 8
  ouo.io bypass were NOT picked — do not build them unprompted).
  Ideas only, reimplemented in our own code — WZML-X is AGPL-3.0,
  so nothing is copied from it, and torrent features are excluded
  entirely (moon: torrent may risk the HF account). (1) **Balanced
  split** (`core/splitter.py`): parts are near-equal
  (`ceil(size / parts)`, each at most `SPLIT_PART_BYTES`) instead
  of fixed 1.95 GB chunks, so a file barely over the cap becomes
  two even halves. (2) **FloodWait retry** (`core/sender.py`):
  every send rides out short Telegram FloodWaits (sleep + retry,
  max 5 waits, gives up past a 10-minute wait) instead of failing
  the job. (3) **yt-dlp hardening**: `retries` 3 → 10,
  `fragment_retries: 10`, `trim_file_name: 220`. (4) **Split
  parts as one media group**: consecutive `.001`/`.002` parts go
  out as a single album (chunked at 10). (5) **New host
  resolvers**: mediafire, gofile, hubcloud/hubdrive/gdflix
  (`core/extractors/mediafire.py`, `gofile.py`, `hubcloud.py`,
  registered after `filehosts`); hubcloud probes candidates
  with a 1-byte Range GET, never HEAD (R2 presigned URLs are
  GET-signed — HEAD 403s). Gdflix live flow + pack support are
  implemented but UNVERIFIED against the live site (gdflix
  file pages 500d during testing; no live pack link found).
  (6) **Richer captions**: uploads gain a size/duration/quality
  line (total size across all paths; duration + resolution from
  ffprobe). (7) **Terabox BDUSS**: `TERABOX_BDUSS` env var —
  either cookie alone enables the cookie route; both set means
  both sent. Verified: 60 checks in /tmp/test_v132.py + 31
  replay checks in /tmp/test_v132_replay.py (real curl-fetched
  pages through the modules' real code; sandbox aiohttp cannot
  reach live sites, so one live-bot retest per new host is
  still the final proof).
- v1.31.0 adds a **Reddit OAuth extractor**
  (`core/extractors/reddit_oauth.py`): Reddit posts go through
  Reddit's official API (app-only bearer token from a free "script"
  app) instead of the scrape Reddit blocks on datacenter IPs — no
  cookie jar needed, one API call for the post's media URLs. Video
  via `media.reddit_video.dash_url` (yt-dlp merges the DASH
  tracks), galleries via `gallery_data` + `media_metadata`, images
  direct, GIFs via the preview mp4 variant; crossposts fall back to
  the parent. Needs `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`
  (Space Secrets); when unset the extractor doesn't match and
  Reddit stays on gallery-dl + cookies exactly as before. Any
  failure before media is confirmed raises `FallThrough`
  (`player_scan.py`) — both dispatch sites catch it and continue
  the chain, so a Reddit link to a YouTube video still reaches
  yt-dlp. Live proof still needs moon's Reddit app + a device test
  (no credentials exist yet to test the API path from the host).
- v1.30.0 adds two HF-only features (both were impossible on Koyeb's
  2 GB disk / 512 MB RAM). (1) **Byte-split downloads**
  (`core/splitter.py`): a finished download over the 2 GB Telegram cap
  is chopped into ~1.95 GB `.001`/`.002` parts (7-Zip naming) and sent
  as documents, up to a 20 GB total (`SPLIT_MAX_TOTAL_BYTES`). Parts
  are NOT playable — they must be joined back (7-Zip, `cat`) before
  watching, unlike /sdl's time segments. `check_size(allow_split=True)`
  admits oversize files only when 2x the file fits on disk (the split
  briefly holds file + parts); `BIG_FILE_LOCK` serialises the
  download+split window of oversize jobs; `maybe_split` re-checks disk
  room for flows that never knew the size (gallery-dl, scrape).
  Wired into every uploader flow and sendlink (Check step + after
  download). `sender.send_file` never ffprobe-reclassifies a `.001`
  part as video — the first part still carries the video header.
  `OUTBOUND_CAP_GB` default is now 1000 (HF publishes no egress cap;
  the 100 GB number was Koyeb's — the guard stays as a runaway
  safety). NOTE: the HF Space still has the OLD `OUTBOUND_CAP_GB=100`
  variable set, which overrides the new default until moon updates it.
  (2) **Headless browser scraper** (`core/extractors/browser_scan.py`,
  Playwright + Chromium baked into the Docker image as root with
  `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` so UID 1000 can read the
  binaries; `playwright==1.65.2` pinned in both Dockerfile and
  requirements.txt — keep them in sync): last resort after the static
  scrape and the data: playlist, in both the chat uploader and
  sendlink. Watches the page's own network requests (manifest first,
  then biggest file), falls back to DOM video/audio srcs, then
  `player_scan.find_media` on the rendered HTML. v1.35.0 adds
  click-to-play: when the first load finds no media, the browser
  clicks play controls (up to 3, play buttons only) and re-checks
  after each click — for placeholder players that load nothing until
  clicked. Fails fast and
  honestly on captcha markers — a headless browser on a datacenter IP
  does NOT beat Turnstile/Cloudflare. One browser at a time
  (`BROWSER_LOCK`), 75 s overall cap, `--no-sandbox` +
  `--disable-dev-shm-usage` (non-root container, tiny /dev/shm).
  Playwright is optional at import: without it the module reports
  `BROWSER_AVAILABLE = False` and the bot works as before.
- v1.29.1 adapts the bot for Hugging Face Spaces (moon accepted the
  account-risk and ordered the move on 2026-11-16; the Stremio bot's
  grandfathered Docker Space becomes this bot's home — see
  `HF_DEPLOY.md` for the full checklist). Changes: Dockerfile runs as
  UID 1000 (`useradd -m -u 1000 user`, `WORKDIR $HOME/app`,
  `COPY --chown=user`, `pip install --user` — HF runs containers as
  UID 1000, so a root-owned /app would leave the bot unable to write);
  `config.PORT` defaults to 7860 (HF's routed port; Koyeb sets
  PORT=8080 explicitly, so it is unaffected); README.md carries the HF
  YAML frontmatter (`sdk: docker`, `app_port: 7860`) — never remove it
  or the Space forgets it is a Docker Space; `.dockerignore` added
  (.git, caches, data/downloads/uploads). The same image still runs on
  Koyeb. Full HF rules (secrets, BASE_URL, pinger, stop-Koyeb-first)
  live in `HF_DEPLOY.md`, not here.
- v1.29.0 adds three static media-finding tricks adapted from the Ghost
  Downloader extension's CatCatch scripts (no browser needed): (1) a
  recursive JSON scan (`player_scan.scan_json_media`) — parses
  `application/json` / `ld+json` blocks and walks them for media URLs,
  including tokenised stream links with no file extension, identified
  by their key name (`file`, `hls`, `playbackUrl`, ...); a key-hint
  regex covers JS object literals that are not valid JSON. (2) Hidden
  `data:` HLS playlists (`extract_data_playlist` + `resolve_playlist_urls`)
  — decoded and handed to ffmpeg, which copies the segments into an
  mp4 (`scrape.download_playlist`); ffmpeg needs `-protocol_whitelist
  file,http,https,tcp,tls,crypto,data` because a local playlist may
  only reference file:// segments by default, and http `-headers` are
  REJECTED when the playlist input is a local file. (3) Player-iframe
  scanning (`iframe_srcs`, max 3 frames, ads/social/YouTube/Vimeo
  embeds skipped) — the video link often lives in the framed document,
  not the embedding page; wired into both the scrape fallback and the
  gate walker (after the captcha check, before the gate form).
  Also: `.mpd` now counts as a playlist in `filehosts._result`, and
  `_plausible` is images-aware (a "poster"/"thumb" URL is the prize
  itself on image hosts, not a skip signal).
- v1.28.4 fixes the gate walker against a REAL UpFiles link (anonymous
  upload https://upfiles.com/oe1RxuAu, sample.mp4): (1) `gate_form` scored
  forms on gate fields alone, so the locale-switcher form tied with the
  free-download form (both only carry `_token`) and won as first in the
  HTML — the walker would have submitted it and looped. It now scores
  download signals (form id, download field names like `download_type`,
  button text, posting back to the same page). (2) The free-download
  form leads to a Cloudflare Turnstile captcha, which a browser-free
  walker can never pass — the walk now detects captcha markers
  (Turnstile / reCAPTCHA / hCaptcha) and fails fast with an honest
  "needs a captcha, download it in your browser" error instead of
  looping to the step limit. UpFiles free downloads are NOT bypassable
  by the bot; the captcha is a hard wall. Verified by replaying the
  real saved pages through the walker locally (sandbox aiohttp cannot
  reach the live site — SSL interception — so the real HTML was
  fetched with curl and replayed).
- v1.28.3 adds live speed + ETA to the `/sendlink` job cards, both steps:
  download shows average MB/s since the step started and time left, upload
  shows the same via a new cumulative `progress_cb` on `sender.send_files`
  (optional, backward-compatible; media groups have no live hook, so photo
  sets report each finished group). The done card shows how long the job
  took. Speeds appear only after 1 s of data, so the card never shows a
  wild first-half-second number.
- v1.28.2 fixes a bug found in review of freebuff's branch: GET download
  gates sent their fields as a request body (which servers ignore) instead
  of the query string — a GET gate could never open. `player_scan.request`
  now takes `params` for query fields, and `filehosts._submit` uses it for
  GET forms. Verified against a local GET-form replica. Also fixes the
  AGENT.md version line, which the v1.28.1 commit left at v1.28.0.
- v1.28.1 fixes `/phost`: Catbox stays first, freeimage.host is the
  fallback when Catbox refuses the bot's datacenter IP, and the reply
  names the host that served the file. Both hosts verified live from a
  data-centre IP on 2026-11-11.
- v1.28.0 adds custom extractors for the video hosts and file hosts /
  shorteners moon uses: Streamtape (+ tapeadsenjoyer mirror), Vidara
  (+ its mirrored panels), and a generic browser-free "gate walker" for
  streamfile.net, luluvid.com, torupload.com, d.torupload.com,
  upfiles/upfilesgo, cloudfam.io, myupload.us, flash-files.com and
  imagetwist.com. New files: `player_scan.py`, `streamtape.py`,
  `vidara.py`, `filehosts.py`; one registry line each. Verified live:
  Streamtape page → get_video → tapecontent.net 302 (title + size), and
  Vidara's `/api/stream` JSON endpoint. Every template shape (XFileSharing
  gate, YetiShare `submitted` form, Laravel `_token` form, jwplayer page,
  meta-refresh shortlink, `<img>` image page) is covered by local-replica
  tests, not by live files — no public file code existed for those hosts,
  so moon should retest with a real link and send back the exact error if
  one fails.
- `/sendlink` one-tap rebuild (v1.27.0). Before it:
  v1.25.2 (unzip count cap 50→200), v1.25.3 (menu/convert failures show as
  popups instead of silent nothing), v1.26.0 (first `/sendlink` with a
  quality picker — superseded by v1.27.0's no-picker flow).
- **Live version on Koyeb is never directly confirmed.** Check it via
  `/status`'s version line when it matters.
- **Pending device retests from moon:** `/convert` on v1.25.3 (bot now shows
  the real error if it still fails — he should send the popup text back),
  a real 600/900 MB zip (file count inside still unknown vs the 1000 cap),
  `/linkuploadpage` one-tap flow on v1.27.0 (real video + image links), fileuploadpage from
  Windows, fileuploadpage Upload-to-admin from the page, a Terabox link via FileSnag,
  a luluvido page link, a real direct/HLS/DASH `/pipe`, fresh-link mpvRx
  seeks after v1.18.2.
- **Open decisions waiting on moon:** Terabox ndus route (A. Koyeb API token
  via secure entry / B. he pastes ndus into Koyeb himself / C. drop ndus,
  stay on FileSnag + parallel); `/pipe` split vs cancel pick (1/2/3);
  YouTube "Sign in to confirm you're not a bot" (fix order already given:
  `/updateytdlp` → fresh logged-in cookies → different Google account).
- UPage audit findings (pending_bytes undercount, no per-chunk space
  re-check, 1 h-from-creation sessions) — fixes NOT approved, do not fix
  unprompted.

## Do NOT build (unless moon reopens or picks)

- `/pipe` split (`.001` parts) and `/pipe` cancel — designs ready, pick open.
- `/pipe` video mode / pipe inside normal uploaders — shelved by moon.
- Mega support — moon said "leave mega now".
- Rename, send-as-document/video override as a standalone feature, remux,
  subscription tokens, aria2, JDownloader, HF migration (bot stays on
  Koyeb), per-account destination (bot-wide per feature stands).
- Old unpicked pitches (do not re-pitch as new): confirm-before-big-download,
  multi-link queue, remember-my-quality, retry button; playlist downloader,
  subtitles-only mode, clip maker, clean filenames, send-to-both-destinations,
  scheduled downloads; saved-links, link health check, Malayalam subtitle
  search via MalSub, send-to-other-account, link unshortener, per-link
  file-to-link expiry; `/history` + duplicate warning, `/speedtest`, weekly
  yt-dlp auto-update, per-user usage breakdown, `/setndus`.
- Parked topics, no nudges: TeleStremio, Lesson 2, Reddit bot, bot `/live`
  while the Ghost phone path is live, the file-to-link web-player route idea.

## Working with other agents

- There is no strict lane split on this repo (unlike MoonStream's
  TDLib/backend split). Any agent may work on any part — but it must follow
  this file, and it must not contradict a decision recorded here without
  asking moon first.
- Moon sometimes relays `.patch` files from another AI (Claude, freebuff,
  Antigravity). The receiving agent's job: compile-check the patch, test it
  (see Testing), fix what breaks, and only then commit/push. A patch is not
  verified until it is tested — never report it as done on the strength of
  the patch alone.
- If two agents disagree, moon decides. Do not rewrite this file to win an
  argument; propose the change to moon and update it only when he agrees.
- When you finish a change, update the "Current status" section above and
  the dated notes pattern the assistant keeps (`~/memory/<date>.md` is the
  assistant's own log, not repo content — repo history lives in git).
