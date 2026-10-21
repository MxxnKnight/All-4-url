# All-4-URL

> **Personal use only.** A private, owner-locked Telegram bot built for one
> person (its owner). It only responds to the Telegram account whose id is
> set in `OWNER_ID` — everyone else is silently ignored. Do not deploy it
> as a public service.

One bot, four jobs:

1. **URL uploader** — send a link, get the file back in the chat.
2. **File-to-link** — send a file, get an expiring download link and a
   streaming link.
3. **Restricted downloader** — send a `t.me` link to a post you can see but
   can't forward/save, get an unrestricted copy.
4. **MediaInfo** — full metadata for any file, extracted from just the
   first few MB and published as a Telegraph page.

`/start` opens a small button menu in the chat — sections for each
feature, reactions (👀 working / 👍 done / 👎 failed) on your link
messages, and a 📊 MediaInfo button under uploaded files.

---

## How the uploader decides (resolver chain)

```
link ─┬─ Terabox domain ──────────────► Terabox extractor (needs ndus cookie)
      ├─ Instagram /p/ (photos) ────────► yt-dlp metadata (carousel images)
      │                                   └► gallery-dl ► yt-dlp ► scrape
      ├─ Instagram /reel/, most sites ──► yt-dlp (quality buttons when the
      │                                   site offers several resolutions)
      ├─ URL that *is* a file ──────────► direct streamed download
      └─ anything left ─────────────────► gallery-dl ► page scrape
                                          (og:video / og:image / .m3u8)
```

Engines: **yt-dlp** (1000+ video/audio sites), **gallery-dl** (Instagram
images/carousels, X image posts, Reddit galleries, Pinterest, Pixiv…),
a hand-written **Terabox** extractor (yt-dlp refuses Terabox upstream),
a **direct-file** streamer, and an OpenGraph **scraper** as last resort.

---

## Setup

### 1. Telegram credentials

| What | Where |
|---|---|
| `BOT_TOKEN` | Talk to **@BotFather** → `/newbot` → copy the token |
| `API_ID`, `API_HASH` | <https://my.telegram.org/apps> → create an app |
| `OWNER_ID` | Your numeric user id — message **@userinfobot** |

### 2. Session string (only for the restricted downloader)

On your own computer:

```bash
pip install wzgram python-dotenv
python gen_session.py
```

It logs in with your phone number + the code Telegram sends (+ 2FA
password if set) and prints one long string. That string **is** a login
to your account: put it only in the `SESSION_STRING` env var on the host.
The account must be a **member** of any private chat you want to pull
from — the bot cannot join chats for it.

### 3. File-to-link: just BASE_URL (storage channel optional)

File-to-link works **in the bot PM, with no channel**: send a file to
the bot and the reply's links point back at that very message — when a
link is opened, the bot streams the file out of your chat. Two rules
follow from that design:

- `BASE_URL` (your deployment's public URL) is the one setting the
  feature needs.
- **Don't delete the file message** — the links read from it, so they
  break when it goes away.

If you want links that survive deleting the original, enable the
optional permanent mode:

1. Create a private channel, e.g. `All-4-URL storage`.
2. Add the bot to it and make it an **admin** (it needs to post).
3. Get the channel's id: forward one of its messages to an id-helper bot,
   or open the channel in Telegram Web — the id looks like `-100…`.
4. Set `STORAGE_CHANNEL_ID` to it. Files are then copied into the
   channel first and the links point at the stored copy.

Either way the host's disk never stores files, which matters because
free hosts wipe their disks on every restart.

### 4. Deploy on Koyeb (recommended, free)

1. [app.koyeb.com](https://app.koyeb.com) → **Create App → Web Service →
   GitHub** → pick `MxxnKnight/All-4-url` (this private repo).
2. Builder: **Dockerfile**. Instance: the free **Nano**.
3. Port: `8080`. Health check path: `/health`.
4. Environment variables (from `.env.example`): `API_ID`, `API_HASH`,
   `BOT_TOKEN`, `OWNER_ID`, plus the optional `SESSION_STRING`,
   `STORAGE_CHANNEL_ID` (permanent file-to-link), `TERABOX_NDUS`.
5. Deploy. When it's live, copy the `https://….koyeb.app` address and set
   it as `BASE_URL`, then redeploy once — file-to-link needs it to build
   its links, and the image needs the `mediainfo` package (already in
   the Dockerfile) for /mediainfo.

Koyeb free gives ~512 MB RAM, 100 GB outbound traffic and — crucially —
**no sleep**. The bot is tuned for exactly that (max 2 jobs at once,
chunked streaming everywhere, progress edits throttled to ~3.5 s).

**Render alternative:** `render.yaml` is included, but Render free
**sleeps after ~15 min idle** with 30–60 s cold starts, so the bot feels
dead often. Prefer Koyeb.

### 5. Run locally (development)

```bash
cp .env.example .env   # fill it in
pip install -r requirements.txt
python main.py
```

Requires Python 3.11+ and `ffmpeg` on the PATH.

---

## Cookies (when login-walled sites fail)

Instagram increasingly demands a login for photo posts, and Reddit
blocks server IPs outright ("blocked by network security") — both are
fixed the same way: give the bot your logged-in browser cookies.

**How:** tap `/setcookies` and pick the site (YouTube, Instagram,
Reddit, X, TikTok, Facebook — or 🔍 Auto-detect). The bot asks for the
file; send the `cookies.txt` and it **verifies the file really holds
that site's cookies** before installing. Each site gets its own jar
(`cookies/instagram.txt`, …), so refreshing one never touches the
others and no site ever sees another site's cookies. Sending a newer
file for the same site just updates it. `/cookies` lists every jar
with cookie counts and install dates.

Fast paths still work: reply `/setcookies` to a cookies.txt, or send
one with `/setcookies` as the caption — the site is auto-detected
from the file.

To make the file: in a desktop browser where you're logged into the
site, install a "Get cookies.txt" style extension and export cookies
for the site (Netscape format). On Android, Kiwi Browser supports
desktop Chrome extensions.

Manual alternative: drop per-site files into `cookies/` yourself
(`<site>.txt`, git-ignored — never commit them), or keep one legacy
`cookies.txt` / `COOKIES_FILE` as a fallback for every site.

Cookies expire eventually, so when sites start failing again, just
re-send a fresh file — treat cookies as refreshable, not permanent. A
throwaway account's cookies are safer than your main account's.

### Surviving redeploys (free hosts wipe the disk)

Free hosts wipe the container disk on every redeploy — normally that
would delete your cookie jars, the `/addsudo` sudo list and the usage
stats. The bot backs each of those up the moment it changes and
restores anything missing on startup (usage stats are backed up on
shutdown), so everything survives redeploys with **zero setup**.

Where the backups live: the `STORAGE_CHANNEL_ID` private channel when
you've set one (invisible, tidiest), otherwise right in your PM with
the bot — you'll see small `#all4url-backup` documents appear there;
leave them alone (pin them yourself if you want them tidy, the bot
finds them by tag either way).

**With MongoDB enabled (next section), none of this is sent at all** —
the database is the durable store, and the vault only remains as the
safety net for running without a database.

### MongoDB storage (recommended)

Set `DB_URI` (your MongoDB connection string, e.g. from MongoDB Atlas)
and `DB_NAME` (this bot's own database name on that cluster — the same
URI can serve all your bots, each with its own `DB_NAME`) and the
bot's durable data moves into MongoDB:

| Collection | Holds |
|---|---|
| `sudo` | the runtime sudo users added with `/addsudo` |
| `usage` | bandwidth + job counters per day (`2026-10-21`), per month (`2026-10`) and all-time (`total`) — downloaded, uploaded and served bytes |
| `cookies` | the per-site cookie jars from `/setcookies` (the owner uses throwaway accounts for these — anyone with the DB URI could use the sessions, so treat the URI like a password) |
| `settings` | bot settings changed from chat — currently the upload destinations |

The 📈 Usage screen shows today / this month / all time from it.
Without `DB_URI` the bot keeps using its local files (with the
PM/channel backup vault as their safety net) — Mongo is an upgrade,
never a requirement.

### Bandwidth guard

Outbound traffic (uploads to Telegram + file-to-link serves) is what
free hosts meter. The bot watches this month's outbound against
`OUTBOUND_CAP_GB` (default 100): at `BANDWIDTH_WARN_PCT` (80%) it PMs
the owner a warning; at `BANDWIDTH_STOP_PCT` (95%) it pauses new
download jobs (uploader + restricted + batch) until the 1st —
streaming links keep working. Thresholds are env-tunable.

## Updating yt-dlp

Send `/updateytdlp` — the bot upgrades yt-dlp in place (pip) and the
new version is active from the next download, no redeploy needed. On
Render free this lasts until the next redeploy; for a permanent bump,
the `yt-dlp` pin in `requirements.txt` needs updating too.

## Terabox cookie (`ndus`)

Terabox's share API refuses anonymous list calls without an account
cookie:

1. Log in at [terabox.com](https://www.terabox.com) in a desktop browser.
2. Press `F12` → **Application** tab → **Cookies** → `terabox.com`.
3. Copy the value of the cookie named **`ndus`**.
4. Set it as the `TERABOX_NDUS` env var and restart the bot.

Cookies expire eventually; when Terabox links start failing with a
cookie error, repeat the steps.

---

## MediaInfo

Send media and ask for its metadata in any of three ways:

1. Reply to a media message with `/mediainfo`.
2. Send a file with the caption `/mediainfo`.
3. Tap **📊 MediaInfo** under a file-to-link reply or an uploaded file.

The bot does **not** download the whole file: it streams only the
first ~12 MB (containers keep their metadata at the head) and, for
MP4/MOV/MKV files where the head says nothing, the last ~4 MB too
(the `moov` atom often lives at the end) — never more than ~16 MB in
total. It runs the **MediaInfo** CLI over that probe (ffprobe is the
fallback), replies with a short summary (format, duration, video /
audio / subtitle lines), and publishes the **complete** report — every
field of every track — as a **Telegraph** page behind the
*📊 Full MediaInfo ↗* button. If Telegraph is unreachable, the full
report arrives as a `mediainfo.txt` document instead.

---

## Split downloads (`/sdl`)

For a direct-link video that's too big to fetch whole (Telegram's
2 GB cap — and this host's small disk):

    /sdl <direct video link>

The bot **probes first and downloads nothing**: it checks the link
is really a direct file, reads the video's duration/resolution/
bitrate remotely, and verifies the host supports **range/seek**
requests. You get a report card — total size, duration, and the
part plan — with a **✂️ Start splitting** button.

Each part obeys **both** caps (`SEGMENT_MAX_MIN`, default 45
minutes, and `SEGMENT_MAX_MB`, default 1000 MB): the cutter
(ffmpeg, stream-copy, all audio/subtitle tracks kept) reads only
that part's time window from the remote file, measures the real
result, and re-plans the next part from the measured bitrate —
variable-bitrate files self-correct, and an over-cap part is
re-cut shorter before upload. Parts arrive as **playable videos**
in your chosen upload destination. Only one split job runs at a
time; free disk is checked before every part; the monthly
bandwidth guard can stop a job between parts (sent parts stay
sent). Files up to `SEGMENT_MAX_TOTAL_GB` (default 8 GB). Honest
limits: cut points snap to keyframes (a few seconds' drift), and
hosts without range support can't be split this way.

## Control panel (Mini App)

The bot has a Telegram Mini App control panel — same Koyeb web
server, no extra hosting. After deploying with `BASE_URL` set, open
`/start`: the **🎛 Control Panel** button opens it (the bot also
sets the chat menu button next to the text box to the panel on
startup). If an old bot on this token left *its* menu button
behind, BotFather can override: `/mybots` → Bot Settings → Menu
Button.

Inside, styled like a normal Telegram Mini App:

- **📊 Usage** — today / month / all-time numbers and the monthly
  outbound bandwidth meter.
- **🍪 Cookie jars** — which sites have jars, plus **paste-to-
  install**: pick the site, paste the full `cookies.txt` content,
  tap Install. Same checks as the file flow (right site's cookies,
  login-cookie warning), saved to the database like any jar.
- **🎯 Upload destination** — Bot PM / Saved Messages toggles for
  URL uploads and restricted downloads (same setting as
  `/destination`).

The panel is **owner-only**: every request is verified against
Telegram's signed Mini App data, and only the owner's account gets
in.

## Usage

| Send this | Get this |
|---|---|
| A YouTube/TikTok/X/Reddit/… link | Quality buttons (when offered), then the video — or an mp3 via *Audio only* |
| An Instagram reel link | The reel video |
| An Instagram photo/carousel link | All the photos |
| A Terabox share link | A file picker (if several files), then the file |
| Any direct file link | The file |
| A file (video/document/photo/audio) | Download + stream link buttons pointing back at your message, valid `LINK_EXPIRY_HOURS` (default 72 h) — don't delete the message |
| A file with caption `/mediainfo`, a reply with `/mediainfo`, or `/mediainfo <link>` (direct file URL) | A metadata summary + a sectioned Telegraph page (📌 file, 🗒 General, 🎞 Video, 🔊 Audio, 🔠 Subtitles) with the full MediaInfo report (partial probe — only the first bits are fetched, never the whole file) |
| 📈 **Usage** button in `/start` | Uptime, downloaded / uploaded / served bandwidth (this month + all time), outbound vs the 100 GB free allowance, and per-feature job counts |
| `/start` or `/help` | The button menu (Uploader / File-to-Link / Restricted / MediaInfo / Usage) — all commands also live in Telegram's `/` commands menu, registered by the bot itself on startup |
| `/addsudo`, `/delsudo`, `/sudos` | Owner only: add/remove/list sudo users (full bot access, same as you). Permanent sudo users go in the `SUDO_USERS` env var; `/addsudo` ones last until the next redeploy on hosts with ephemeral disks |
| `/setcookies` | Owner only: tap and pick the site, then send its `cookies.txt` — verified and installed as that site's own jar (reply-to-file and `/setcookies` caption still auto-detect). Fixes Instagram/Reddit login walls |
| `/cookies` | Owner only: list installed cookie jars with counts and dates |
| `/destination` | Owner only: choose where finished files are delivered — 💬 Bot PM or 💾 your own Saved Messages (via the user session) — separately for URL uploads and restricted downloads |
| `/updateytdlp` | Owner only: upgrades yt-dlp in place, active from the next download — no redeploy needed (lasts until redeploy on Render free) |
| A `t.me/…` post link | An unrestricted re-upload of that post; media-group albums re-upload as albums |
| `/batch` then one message with many t.me links (or links right in the `/batch` message) | 2+ links: a content-type filter menu (toggle text/photos/videos/… on/off), then a delay menu (3–60 s between items), then the batch runs. Exactly two links to the same chat = a *range*: first-link…last-link walks every message between them (deleted ones counted silently, capped at 100/batch). The same range also works as ONE Destiny-style link — `t.me/chat/111-115` |
| `/cancel` | Stops a running batch after the current item |
| `/help` | Short in-bot help |

## Library note: WzGram ⇄ Kurigram

This bot runs on **WzGram** (`wzgram==3.1.5`), a maintained fork of the
discontinued Pyrogram — the same setup the fast DestinyTgRestrict bot
uses. Code imports only `pyrogram`, which WzGram provides. To switch to
**Kurigram**, edit `requirements.txt`: delete the `wzgram==3.1.5` line
and add `kurigram` (both ship a module named `pyrogram`, so never install
both at once). No code changes are needed.

## Limits & honest caveats

- **2 GB** is Telegram's ceiling for bot uploads. Anything bigger is
  refused up front with a clear message instead of dying mid-download
  (`MAX_FILE_SIZE` env, default 2 GB); the same guard also refuses a
  download that wouldn't fit the server's free disk space.
- **Koyeb's 100 GB/month** outbound counts every upload *and* every
  file-to-link download. Heavy Terabox/large-file use eats it fast.
- **Range/seek:** file-to-link streams answer HTTP Range requests with
  byte-exact 206 responses, so players can seek. RAM stays flat (256 KiB
  slices) no matter the file size — built for the free tier.
- **Terabox is fragile**: its unofficial share flow changes occasionally
  and cookies expire; when it breaks, the extractor in
  `core/extractors/terabox.py` is the one file to re-check.
- **Instagram** without cookies will fail on many photo posts — that's
  Instagram's login wall, not a bug.
- Downloads happen on a 0.1 vCPU free box: large files work, but they
  are not fast, and only 2 jobs run at once by design.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Bot online but never replies | Your id isn't `OWNER_ID`, or `OWNER_ID` is wrong |
| "SESSION_STRING is not set" | Restricted feature needs the session (Setup §2) |
| Restricted post fails "message does not exist" | The user account isn't a member of that chat |
| File sent, bot asks for BASE_URL | `BASE_URL` isn't set — it's the only thing PM file-to-link needs |
| File-to-link links 404 after working before | The original file message was deleted (PM mode) — or, in channel mode, the stored copy was |
| Channel mode copy fails | `STORAGE_CHANNEL_ID` wrong or the bot isn't a channel admin |
| Links in file-to-link point nowhere | `BASE_URL` isn't the public Koyeb URL |
| /mediainfo finds no streams on an MP4 | Metadata sits in a tail `moov` — the probe already retries with the tail; if it still fails, the file is likely truncated/corrupt |
| /mediainfo summary works but report is a .txt file | Telegraph was unreachable at that moment; the full report is the attachment |
| Everything got slow / OOM | Someone raised `MAX_JOBS`, or giant files — keep it at 2 |
| Terabox: jsToken / cookie error | `ndus` cookie expired — redo the cookie steps |
