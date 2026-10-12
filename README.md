# All-4-URL

> **Personal use only.** A private, owner-locked Telegram bot built for one
> person (its owner). It only responds to the Telegram account whose id is
> set in `OWNER_ID` — everyone else is silently ignored. Do not deploy it
> as a public service.

One bot, three jobs:

1. **URL uploader** — send a link, get the file back in the chat.
2. **File-to-link** — send a file, get an expiring download link and a
   streaming link.
3. **Restricted downloader** — send a `t.me` link to a post you can see but
   can't forward/save, get an unrestricted copy.

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

### 3. Storage channel (only for file-to-link)

1. Create a private channel, e.g. `All-4-URL storage`.
2. Add the bot to it and make it an **admin** (it needs to post).
3. Get the channel's id: forward one of its messages to an id-helper bot,
   or open the channel in Telegram Web — the id looks like `-100…`.
4. Set `STORAGE_CHANNEL_ID` to it.

Files are copied into this channel and streamed back out of it — the
host's disk never stores them, which matters because free hosts wipe
their disks on every restart.

### 4. Deploy on Koyeb (recommended, free)

1. [app.koyeb.com](https://app.koyeb.com) → **Create App → Web Service →
   GitHub** → pick `MxxnKnight/All-4-url` (this private repo).
2. Builder: **Dockerfile**. Instance: the free **Nano**.
3. Port: `8080`. Health check path: `/health`.
4. Environment variables (from `.env.example`): `API_ID`, `API_HASH`,
   `BOT_TOKEN`, `OWNER_ID`, plus the optional `SESSION_STRING`,
   `STORAGE_CHANNEL_ID`, `TERABOX_NDUS`.
5. Deploy. When it's live, copy the `https://….koyeb.app` address and set
   it as `BASE_URL`, then redeploy once — file-to-link needs it to build
   its links.

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

## Instagram cookies (when posts fail)

Instagram increasingly demands a login for photo posts. To give the bot
your (throwaway, ideally) Instagram login:

1. In a desktop browser where you're logged into Instagram, install a
   "Get cookies.txt" style extension.
2. On instagram.com, export cookies for the site — Netscape format.
3. Save the file as `cookies.txt` next to `main.py` locally, **or** on
   Koyeb add its full contents in a way your deployment mounts as a file
   (it is git-ignored — never commit it; note a redeploy wipes the disk,
   so treat cookies as refreshable, not permanent).

Both yt-dlp and gallery-dl pick the file up automatically.

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

## Usage

| Send this | Get this |
|---|---|
| A YouTube/TikTok/X/Reddit/… link | Quality buttons (when offered), then the video — or an mp3 via *Audio only* |
| An Instagram reel link | The reel video |
| An Instagram photo/carousel link | All the photos |
| A Terabox share link | A file picker (if several files), then the file |
| Any direct file link | The file |
| A file (video/document/photo/audio) | Download + stream links, valid `LINK_EXPIRY_HOURS` (default 72 h) |
| A `t.me/…` post link | An unrestricted re-upload of that post (albums included) |
| `/batch` then one message with many t.me links (or links right in the `/batch` message) | Each post re-uploaded in order, 3 s apart |
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

- **2 GB** is Telegram's ceiling for bot uploads. Bigger results will
  fail at the upload step.
- **Koyeb's 100 GB/month** outbound counts every upload *and* every
  file-to-link download. Heavy Terabox/large-file use eats it fast.
- **Range/seek:** file-to-link streams answer from the start of the file
  (the library's `stream_media` offsets are chunk-based, not byte-based),
  so players can play but generally can't seek. Download links are exact.
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
| File sent, bot says "not configured" | `STORAGE_CHANNEL_ID` missing or bot isn't a channel admin |
| Links in file-to-link point nowhere | `BASE_URL` isn't the public Koyeb URL |
| Everything got slow / OOM | Someone raised `MAX_JOBS`, or giant files — keep it at 2 |
| Terabox: jsToken / cookie error | `ndus` cookie expired — redo the cookie steps |
