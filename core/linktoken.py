"""Signed, expiring tokens for file-to-link URLs.

A token carries everything the web server needs to find the stored file
(storage chat id + message id) plus an expiry timestamp. It is signed
with an HMAC keyed by the BOT_TOKEN, so links cannot be forged or edited
without invalidating the signature, and no database is needed — the
state lives inside the token itself (handy on an ephemeral free host).
"""

import base64
import hashlib
import hmac
import json
import time

import config


class TokenError(Exception):
    """Bad signature, malformed token, or expired link."""


def _secret() -> bytes:
    return (config.BOT_TOKEN or "all4url").encode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(chat_id: int, message_id: int, file_name: str,
               file_size: int = 0, mime: str = "") -> str:
    payload = {
        "c": chat_id,
        "m": message_id,
        "n": file_name,
        "s": file_size,
        "t": mime,
        "exp": int(time.time()) + config.LINK_EXPIRY_HOURS * 3600,
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def parse_token(token: str) -> dict:
    try:
        body, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        raise TokenError("malformed token")
    expected = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise TokenError("bad signature")
    try:
        payload = json.loads(_unb64(body))
    except Exception:
        raise TokenError("malformed payload")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise TokenError("link expired")
    return payload
