# Deploying All-4-URL on Hugging Face Spaces

The bot runs on moon's **grandfathered free Docker Space** (created before
HF started requiring a paid plan to create Docker Spaces). One Space, one
bot: the Stremio bot that used to live there was removed to make room.
**Do not create a second Space for this bot** — new Docker Spaces need
HF PRO ($9/mo).

## What HF needs that Koyeb did not

- **Port 7860.** HF routes only to the Space's `app_port` (7860, declared
  in the README frontmatter). `config.PORT` defaults to 7860; set
  `PORT=7860` in the Space variables anyway, to be explicit.
- **User ID 1000.** The container runs as UID 1000, not root. The
  Dockerfile creates that user, works in `/home/user/app`, copies with
  `--chown=user`, and installs Python packages with `pip install --user`
  (console scripts land in `~/.local/bin`, which is on PATH). A
  root-owned `/app` would leave the bot unable to write downloads, data,
  or job dirs. The same image still runs on Koyeb.
- **README frontmatter.** The YAML block at the top of `README.md`
  (`sdk: docker`, `app_port: 7860`) is the Space config — without it the
  Space forgets it is a Docker Space. Never remove it. GitHub renders it
  as plain text; that is cosmetic only.
- **Secrets, not files.** All credentials go in Space Settings →
  Variables and secrets (runtime env vars). Never commit a `.env`, and
  never put secrets in the Space repo (free Spaces are public).

## Deploy checklist (moon does the dashboard steps)

1. **Back up the old Space first.** Clone the Space repo somewhere safe
   before pushing over it — the Stremio bot code lives only there.
2. **Push this repo to the Space remote**
   (`https://huggingface.co/spaces/<user>/<space>`). The push triggers a
   rebuild automatically. Needs an HF token with write access to the
   Space (fine-grained token scoped to that Space is the safe choice).
3. **Set the variables/secrets in Space Settings** (same values as
   Koyeb — copy them, do not retype secrets from memory):
   - Secrets: `API_ID`, `API_HASH`, `BOT_TOKEN`, `SESSION_STRING`
     (if used), `DB_URI` (if used), `TERABOX_NDUS` (if used),
     `TERABOX_BDUSS` (if used — the same account's Baidu login
     cookie; either cookie alone enables the Terabox cookie route),
     `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` (if used — Reddit
     app login for the OAuth extractor; without them Reddit stays
     on gallery-dl + cookies).
   - Variables: `OWNER_ID`, `SUDO_USERS` (if any), `DB_NAME=all4url`,
     `PORT=7860`, `BASE_URL=https://<user>-<space>.hf.space`
     (no trailing slash, no `/app`), `LINK_EXPIRY_HOURS=72`,
     `OUTBOUND_CAP_GB=1000`, `MAX_JOBS=2`.
   - `BASE_URL` must be the Space's own `*.hf.space` address, or
     file-to-link / Mini App / upage / sendlink URLs will point at
     the wrong host.
4. **Repoint the keep-alive pinger** (cron-job.org) from the Koyeb
   `/health` URL to `https://<user>-<space>.hf.space/health`.
   Free hardware sleeps after inactivity; the pinger is what keeps
   the bot awake, same as on Koyeb.
5. **STOP the Koyeb service before the HF bot goes live.** Two
   instances polling Telegram with the same bot token fight over
   updates (missed and duplicated messages). Only one may run.
   Keep the Koyeb service stopped — not deleted — until HF has
   proven itself for a few days; it is the fallback.
6. **Verify on the live bot:** `/status` answers, a link downloads,
   a file-to-link Stream link plays, `/sendlink` opens. Old Koyeb
   file-to-link URLs stop working the moment Koyeb stops — that is
   expected; new links come from the HF address.

## Things that do NOT change on HF

- MongoDB (`DB_URI`) still holds sudo users, stats, and cookie jars —
  set the same URI and the bot picks up where it left off.
- Disk is ephemeral (50 GB instead of Koyeb's 2 GB); the bot's own
  1 GB caps and per-job cleanup stay as they are.
- Outbound bandwidth has no published HF quota, but the bot's
  `OUTBOUND_CAP_GB` guard stays on — heavy use is what draws
  moderator attention to a Space.
- Telegram connectivity (Bot API + MTProto on port 443) is proven:
  the Stremio bot already talked to Telegram from this same Space.

## Known risk (moon's call, 2026-11-16)

HF's Content Policy bans IP-infringing content and platform abuse;
enforcement can reach the whole HF account (which also holds the
ZeroGPU Spaces and the Roop plans), not just this Space. Moon accepted
that risk explicitly. Keep the bot personal-use only (it already is:
owner + sudo, everyone else silently ignored) and do not advertise
the Space.
