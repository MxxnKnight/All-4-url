"""Startup update notification: "the bot got updated".

On every startup the bot checks whether its version changed
since the last time it told anyone. Only when it changed does
it message the owner and every sudo user, once each:

  ✅ Bot updated to vX.Y.Z
  The bot just finished a new deployment and is back online.

Why version-gated, not "on every startup": Koyeb restarts the
bot for reasons other than a deployment (a crash, a scale
event), and a message on every restart would be spam. A
deployment always bumps BOT_VERSION in config.py, so a
version change IS a deployment, as far as the bot can tell.

Where the last-notified version lives: the Mongo `settings`
document when DB_URI is set (survives redeploys, which is
exactly when this matters), otherwise a small local file in
data/ (which survives restarts within one instance, and is
wiped on redeploy — which also correctly triggers a notice
on the next boot). A failed send to every recipient leaves
the stored version unchanged, so the notice retries on the
next startup instead of being silently lost; a send to at
least one recipient counts as delivered for this purpose.

This module must never break startup: main.py calls it
inside a try/except, and every send is individually guarded.
"""

import json
import logging
import os
import tempfile

import config
from core import db, sudo

log = logging.getLogger("all4url")

DATA_FILE = os.path.join(config.BASE_DIR, "data", "notified_version.json")


def _read_file() -> str | None:
    try:
        with open(DATA_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("version"), str):
            return data["version"]
    except Exception:
        pass
    return None


def _write_file(version: str) -> None:
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(DATA_FILE),
                                   prefix=".notified", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": version}, handle)
        os.replace(tmp, DATA_FILE)
    except Exception as exc:
        log.warning("could not save notified version: %s", exc)


async def notify_if_updated(app) -> bool:
    """Send the update notice if BOT_VERSION changed. True if sent."""
    current = config.BOT_VERSION
    mongo = await db.load_settings()
    if mongo is not None:
        stored = mongo.get("notified_version")
    else:
        stored = _read_file()
    if stored == current:
        return False
    env_ids, runtime_ids = sudo.list_sudo()
    recipients = {config.OWNER_ID} | set(env_ids) | set(runtime_ids)
    recipients.discard(0)
    recipients.discard(None)
    text = (f"✅ Bot updated to v{current}\n\n"
            f"The bot just finished a new deployment and is back "
            f"online. Send /status to check it, or just send a link "
            f"as usual.")
    sent = 0
    for user_id in sorted(recipients):
        try:
            await app.send_message(user_id, text)
            sent += 1
        except Exception as exc:
            log.warning("update notice to %s failed: %s", user_id, exc)
    if sent:
        if mongo is not None:
            await db.save_setting("notified_version", current)
        else:
            _write_file(current)
        log.info("update notice sent for v%s to %d recipient(s)",
                 current, sent)
        return True
    log.warning("update notice for v%s sent to nobody — will retry "
                "on next startup", current)
    return False
