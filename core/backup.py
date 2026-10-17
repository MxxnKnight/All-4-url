"""Survive ephemeral disks (Render free wipes on every redeploy).

Small data files — cookie jars, data/sudo.json, data/stats.json — are
backed up as documents in the vault chat: the private storage channel
when STORAGE_CHANNEL_ID is set, otherwise the owner's PM with the bot
(no setup needed). One message per file, captioned
`#all4url-backup <kind>`; restored on startup when the local copy is
missing.
"""

import logging
import os
import shutil

import config

log = logging.getLogger("all4url")

BACKUP_TAG = "#all4url-backup"


def _vault_chat():
    """Where backups live: the private storage channel when set,
    otherwise the owner's PM with the bot — no setup needed either way."""
    return config.STORAGE_CHANNEL_ID or config.OWNER_ID


def _caption(kind: str) -> str:
    return f"{BACKUP_TAG} {kind}"


def _local_path(kind: str):
    """Local path a backup kind restores to, or None if unknown."""
    from core import cookies as cookies_mod
    from core import sudo as sudo_mod
    from core import stats as stats_mod
    if kind.startswith("cookies/"):
        name = kind[len("cookies/"):]
        base = os.path.basename(name)
        if not base.endswith(".txt") or base != name:
            return None  # no directories, no funny business
        return os.path.join(cookies_mod.COOKIES_DIR, base)
    if kind == "data/sudo.json":
        return sudo_mod.DATA_FILE
    if kind == "data/stats.json":
        return stats_mod.DATA_FILE
    return None


async def _find_backups(app, kind: str) -> list:
    """Backup messages for kind, newest first."""
    vault = _vault_chat()
    found = []
    try:
        async for msg in app.search_messages(vault, BACKUP_TAG,
                                             limit=50):
            if (msg.document and (msg.caption or "") == _caption(kind)
                    and getattr(msg, "outgoing", True)):
                found.append(msg)
    except Exception as exc:
        log.warning("backup search failed: %s", exc)
    return found


async def backup_kind(app, kind: str, local_path: str) -> bool:
    """Upload local_path as the current backup for kind.

    Old backup messages for the kind are deleted, so the channel holds
    exactly one copy. Returns True on success.
    """
    vault = _vault_chat()
    if not local_path or not os.path.isfile(local_path):
        return False
    try:
        olds = await _find_backups(app, kind)
        await app.send_document(vault, local_path,
                                caption=_caption(kind))
        for old in olds:
            try:
                await app.delete_messages(vault, old.id)
            except Exception as exc:
                log.warning("could not delete old backup %s: %s",
                            kind, exc)
        log.info("backed up %s to vault chat", kind)
        return True
    except Exception as exc:
        log.warning("backup of %s failed: %s", kind, exc)
        return False


async def restore_missing(app) -> list:
    """Download backups for kinds whose local file is missing.

    Returns the kinds restored.
    """
    vault = _vault_chat()
    restored = []
    try:
        async for msg in app.search_messages(vault, BACKUP_TAG,
                                             limit=100):
            caption = msg.caption or ""
            if (not caption.startswith(BACKUP_TAG + " ") or not msg.document
                    or not getattr(msg, "outgoing", True)):
                continue
            kind = caption[len(BACKUP_TAG) + 1:].strip()
            local = _local_path(kind)
            if not local or os.path.isfile(local):
                continue
            try:
                os.makedirs(os.path.dirname(local), exist_ok=True)
                tmp = await app.download_media(msg)
                shutil.move(tmp, local)  # move, not replace: tmp may be
                restored.append(kind)    # on another filesystem
                log.info("restored %s from vault chat", kind)
            except Exception as exc:
                log.warning("restore of %s failed: %s", kind, exc)
    except Exception as exc:
        log.warning("backup restore scan failed: %s", exc)
    return restored
