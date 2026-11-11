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

1. **URL uploader** — paste a link in chat (or on the `/sendlink` web page),
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
- Hosting: **Koyeb free tier** — 512 MB RAM, 0.1 vCPU, 2 GB ephemeral disk
  (wiped on redeploy/restart, no Volumes on free), 100 GB/month outbound
  (uploads count, downloads don't), scales to zero after 1 hour without
  inbound web traffic; a cron-job.org `/health` pinger keeps it awake.
  `BASE_URL`: `https://splendid-dorothea-adarshdarsh-de7fd969.koyeb.app`
  (bare domain, no `/app`, no trailing slash). ffmpeg is on the Koyeb image
  (Dockerfile). Headless browsers cannot run on this tier — do not propose
  browser-based extractors.
- Telegram cap: 2 GB per file (`MAX_FILE_SIZE` = 2,000,000,000).
- Release ritual: bump `BOT_VERSION` in `config.py` → test (see Testing) →
  descriptive commit → push both remotes → verify with `git ls-remote`.
  Docs-only commits do NOT bump the version. On startup the bot sends a
  "Bot updated to vX.Y.Z" notice to the owner + sudo users once per version
  (`core/version_notify.py`) — so a version bump is also a user notification.
- **Current version: v1.28.2** (2026-11-12).

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
| `config.py` | `BOT_VERSION`, `BOT_TOKEN`, `OWNER_ID`, `BASE_URL`, `LINK_EXPIRY_HOURS` (72), `MAX_FILE_SIZE`, `MAX_JOBS` (2), `OUTBOUND_CAP_GB` (100), `TERABOX_NDUS`, `STORAGE_CHANNEL_ID` |
| `core/utils.py` | `JOB_SEMAPHORE`, `ThrottledEditor`, `check_size` / `SizeBlocked`, `make_job_dir`, `cleanup_dir`, `progress_text`, `human_bytes`, `admin_only`, `is_admin_id` |
| `core/sender.py` | `send_files`, `send_files_routed`, `send_file` (re-probes document-typed videos with ffprobe) |
| `core/sudo.py` | Sudo model (below) |
| `core/stats.py` | Bandwidth guard + job counters (`record`, `record_job`, `record_paths`, `bandwidth_blocked`) |
| `core/db.py` | MongoDB when `DB_URI` set, JSON fallback. Collections: `sudo`, `usage`, `cookies`, `settings` |
| `core/linktoken.py` | Signed expiring tokens (HMAC keyed by `BOT_TOKEN`; 43-char signature, **can legitimately start with `-`** — a "bad signature" is usually a truncated copy) |
| `core/webserver.py` | aiohttp server: `/stream/<token>`, `/d/<token>`, `/health`, routes from `webapp`/`upage`/`sendlink` |
| `core/upage.py` | `/upage` 1-hour upload page (see below) |
| `core/sendlink.py` | `/sendlink` permanent link-upload page (see below) |
| `core/pipe.py` | `/pipe` Telegram-direct chunked pipe |
| `core/unzip.py` | One-by-one zip extraction (`zip_info`, `iter_extracted`; caps: zip ≤1 GB, total ≤8 GB, ≤200 files) |
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

- `/upage` (`core/upage.py`, `plugins/upage.py`): **1-hour** page for
  uploading files to the server. Page token carries `uid`. Uploads in 16 MB
  chunks with auto-resume; each finished file gets a single-use signed
  download link (`/u/<token>`). Limits: 750 MB advertised per file, 1 GB
  hard ceiling, 1 GB total pending, 256 MB headroom. Page has "Upload to
  admin" (delivers to OWNER_ID bot PM, bypassing `/destination`) and Delete
  buttons. When any sudo sends a `/u/` link back to the bot, the file goes
  to the owner's bot PM with a preceding note — the sudo gets no copy.
- `/sendlink` (`core/sendlink.py`, `plugins/sendlink.py`): **permanent**
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
- `/sdl`: oversize **direct videos only**, time-split playable parts
  (≤1 GB/part, total ≤8 GB, one part on disk).
- `/phost`: reply to a photo/image → Catbox upload, then freeimage.host as
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

## Current status (2026-11-12) — v1.28.2

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
  a real 600/900 MB zip (file count inside still unknown vs the 200 cap),
  `/sendlink` one-tap flow on v1.27.0 (real video + image links), upage from
  Windows, upage Upload-to-admin from the page, a Terabox link via FileSnag,
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
