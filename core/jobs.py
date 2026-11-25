"""One shared progress message per chat + cancellable jobs (v1.45.0).

Before this, every link got its own status message — send three links
and three progress messages pile up. Now all active URL jobs in one
chat share a SINGLE progress message: each job is one block (label +
its current progress line), and the message carries a Stop button per
job. When a job finishes, its block disappears; when the last job
finishes, the message is deleted. Finished files and per-link reactions
still carry the result, so nothing is lost.

A job's error or final notice is NEVER written into the shared message
(it belongs to all jobs) — `fail()` / `finish(text)` send it as the
job's own message instead. The unzip prompt is likewise its own message
(the prompt's buttons belong to one job only).

Cancel: `Job.cancel()` sets a flag; every stage checks it via
`check_cancelled()` and raises JobCancelled. JobCancelled is a
BaseException on purpose — the download engines treat a plain Exception
from a progress callback as a retryable failure (the parallel
downloader falls back to single-stream), so a normal exception would
NOT stop the job. It mirrors asyncio.CancelledError: `except Exception`
handlers never swallow it, `finally` cleanup still runs, and the
dispatch loop catches it explicitly.

The registry is in-memory only: a Space restart wipes it, same as the
in-flight jobs themselves.

v1.51.6 — rich in-message Stop buttons + re-posting. Telegram's
Bot API 10.3 (Aug 2026) lets a button live INSIDE the message text
(`RichMessageButton` + `InputRichBlockButtons`), so each job now gets
its own small red Stop button right under its progress bar instead of
one long "Stop <label>" row under the whole message. The shared
message is therefore sent as a rich message
(`send_rich_message` / `edit_message_text(rich_text=...)`); if the
rich send fails (old client, server refusal) it falls back to the
plain text + inline keyboard from before, and the edit path falls
back the same way — the job keeps running either way. The tap still
carries `jb:stop:<id>`, so the existing stop handler needs no change.

v1.51.6 also moves the message: it no longer sits at the top of the
chat while files land below it. A new link re-posts it (delete the
old one, send a fresh one at the bottom), and so does a finished
job once its file + final notice are out. Progress updates between
those events still edit in place. Re-posts are spaced at least
2 s apart so a 10-link burst can't flood Telegram — a re-post that
lands inside the spacing just edits in place instead.
"""

import asyncio
import time

#: Minimum seconds between edits of the shared message (Telegram
#: punishes rapid edits; same 3.5 s as ThrottledEditor).
_INTERVAL = 3.5

#: Minimum seconds between re-posts of the shared message (v1.51.6):
#: a multi-link burst registers jobs back to back, and a re-post is
#: a send + a delete, so spacing them keeps Telegram from treating
#: the burst as a flood. A re-post inside the spacing edits in
#: place instead — the message still moves on the next event.
_REPOST_MIN_INTERVAL = 2.0

#: A job id is 8 hex chars: short enough for callback data
#: ("jb:stop:<id>" is 16 bytes) and unique enough for a handful of
#: concurrent jobs.
_counter = 0


class JobCancelled(BaseException):
    """The owner stopped this job. See the module docstring for why
    this is a BaseException, not an Exception."""


class Job:
    """One active job's handle. Flows keep this instead of a status
    message + ThrottledEditor: `edit()` has the same shape as
    ThrottledEditor.edit, so progress call sites barely change."""

    def __init__(self, job_id: str, label: str, state: "_ChatState"):
        self.id = job_id
        self.label = label
        self.line = "Working on it…"
        self.cancelled = False
        self.started = time.time()
        self._state = state
        self._gone = False

    def check_cancelled(self) -> None:
        """Raise JobCancelled when the owner stopped this job."""
        if self.cancelled:
            raise JobCancelled()

    def cancel(self) -> None:
        """Flag the job for stopping (called by the Stop button)."""
        self.cancelled = True
        self.line = "⏹ Stopping…"

    async def edit(self, text: str, force: bool = False) -> None:
        """Set this job's progress line and refresh the shared
        message (throttled). Never raises: progress is cosmetic."""
        self.line = text
        try:
            await _refresh(self._state, force=force)
        except Exception:
            pass

    async def fail(self, text: str) -> None:
        """End the job with an error: drop it from the shared message
        and send the error as its own message."""
        await self._remove(repost=True)
        try:
            await self._state.client.send_message(self._state.chat_id,
                                                  text)
        except Exception:
            pass
        if self._state.jobs:
            # The error is out; move the shared message below it.
            await _repost(self._state)

    async def finish(self, text: str | None = None) -> None:
        """End the job quietly (or with a final notice as its own
        message when `text` is given). Idempotent."""
        await self._remove(repost=True)
        if text:
            try:
                await self._state.client.send_message(self._state.chat_id,
                                                      text)
            except Exception:
                pass
        if self._state.jobs:
            # The file (sent by the flow before finish) and the
            # notice are out; move the shared message below them.
            await _repost(self._state)

    async def _remove(self, repost: bool = False) -> None:
        if self._gone:
            return
        self._gone = True
        state = self._state
        state.jobs.pop(self.id, None)
        if not state.jobs:
            # Last job gone: the shared message has nothing to show.
            await _delete_shared(state)
        elif repost:
            # The caller re-posts after its own message (fail/finish
            # text) so the shared message lands below it; skip the
            # in-place refresh — it would be deleted a moment later.
            pass
        else:
            await _refresh(state, force=True)


class _ChatState:
    """One chat's shared message + its jobs."""

    def __init__(self, client, chat_id: int):
        self.client = client
        self.chat_id = chat_id
        self.jobs: dict[str, Job] = {}
        self.message = None
        #: True when the shared message is a rich message (v1.51.6):
        #: edits must go through edit_message_text(rich_text=...).
        self.rich = False
        self.lock = asyncio.Lock()
        self._last_edit = 0.0
        self._last_text = ""
        self._last_send = 0.0
        self._refresh_task: asyncio.Task | None = None
        self._broken_until = 0.0


#: chat_id -> state. All uploader jobs run in private chats, so one
#: shared message per chat is one per user.
_CHATS: dict[int, _ChatState] = {}


def _next_id() -> str:
    global _counter
    _counter += 1
    return f"{int(time.time()) % 0x10000:04x}{_counter % 0x10000:04x}"


def _render(state: _ChatState) -> str:
    blocks = ["⏳ **Active downloads**"]
    for job in state.jobs.values():
        label = job.label if len(job.label) <= 40 else job.label[:39] + "…"
        blocks.append(f"\n📥 {label}\n{job.line}")
    return "\n".join(blocks)


def _render_blocks(state: _ChatState) -> list:
    """The shared message as rich-message blocks (v1.51.6): a heading
    paragraph, then per job a paragraph (label + progress line) and
    right under it a button row with that job's own small red Stop
    button. The button carries the same `jb:stop:<id>` callback the
    old keyboard did, so the stop handler needs no change."""
    from pyrogram import raw
    from pyrogram.types import (
        InputRichBlockButtons,
        InputRichBlockParagraph,
        RichMessageButton,
    )
    from pyrogram.enums import BlockAlignment, RichButtonStyle

    blocks = [InputRichBlockParagraph(text=raw.types.TextBold(
        text=raw.types.TextPlain(text="⏳ Active downloads")))]
    for job in state.jobs.values():
        label = job.label if len(job.label) <= 40 else job.label[:39] + "…"
        blocks.append(InputRichBlockParagraph(
            text=f"📥 {label}\n{job.line}"))
        blocks.append(InputRichBlockButtons(
            buttons=[RichMessageButton(
                "⏹ Stop", style=RichButtonStyle.DANGER,
                callback_data=f"jb:stop:{job.id}")],
            align=BlockAlignment.LEFT))
    return blocks


def _rich_message(state: _ChatState):
    """Wrap the current blocks in an InputRichMessage for send/edit."""
    from pyrogram.types import InputRichMessage
    return InputRichMessage(blocks=_render_blocks(state))


def _markup(state: _ChatState):
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    for job in state.jobs.values():
        label = job.label if len(job.label) <= 22 else job.label[:21] + "…"
        rows.append([InlineKeyboardButton(
            f"⏹ Stop {label}", callback_data=f"jb:stop:{job.id}")])
    return InlineKeyboardMarkup(rows) if rows else None


async def _send_shared(state: _ChatState, reply_to=None):
    """Send a fresh shared message for the state's current jobs.
    Rich first (v1.51.6, buttons inside the message); plain text +
    the old keyboard when the rich send fails, so a job never loses
    its progress display. Returns the sent message, or None when
    nothing could be sent (retry is blocked for 60 s)."""
    from pyrogram.types import ReplyParameters

    reply_params = None
    if reply_to is not None:
        reply_params = ReplyParameters(
            message_id=getattr(reply_to, "id", None))
    try:
        msg = await state.client.send_rich_message(
            state.chat_id, _rich_message(state),
            reply_parameters=reply_params)
        state.rich = True
    except Exception:
        try:
            msg = await state.client.send_message(
                state.chat_id, _render(state), reply_markup=_markup(state),
                reply_to_message_id=getattr(reply_to, "id", None))
            state.rich = False
        except Exception:
            state._broken_until = time.time() + 60
            return None
    state._last_send = time.time()
    state._last_edit = time.time()
    state._last_text = _render(state)
    return msg


async def register(client, chat_id: int, label: str,
                   reply_to=None) -> Job:
    """Add a job to the chat's shared message (creating the message
    for the first job). Never raises for message failures — the job
    still runs, its progress just isn't shown."""
    state = _CHATS.setdefault(chat_id, _ChatState(client, chat_id))
    job = Job(_next_id(), label, state)
    state.jobs[job.id] = job
    async with state.lock:
        if state.message is None and time.time() >= state._broken_until:
            state.message = await _send_shared(state, reply_to)
            return job
        if state.message is None:
            # Send blocked for now (chat blocked / send failed):
            # don't retry on every registration.
            return job
    # A message already exists: the new link re-posts it (v1.51.6)
    # so it drops to the bottom of the chat instead of sitting at
    # the top while the files land below it.
    await _repost(state)
    return job


def get_job(job_id: str) -> Job | None:
    """Find a job by id across chats (for the Stop button)."""
    for state in _CHATS.values():
        job = state.jobs.get(job_id)
        if job is not None:
            return job
    return None


def snapshot() -> list[dict]:
    """Live jobs for /status: one dict per running job (label, current
    line, age in seconds). Empty when nothing is running — so a /status
    that shows a job stuck for many minutes names the hang instead of
    leaving the bot looking silently dead."""
    now = time.time()
    return [{"label": job.label, "line": job.line,
             "age": int(now - job.started)}
            for state in _CHATS.values()
            for job in state.jobs.values()]


async def _edit_now(state: _ChatState) -> None:
    text = _render(state)
    if text == state._last_text and state.message is not None:
        return
    edited = False
    if state.rich:
        try:
            # Rich message: edit its blocks (same content, buttons
            # included).
            await state.client.edit_message_text(
                state.chat_id, state.message.id,
                rich_text=_rich_message(state))
            edited = True
        except Exception:
            # Rich edit refused (server, old client): fall through
            # to the plain edit, which keeps progress visible
            # without the in-message buttons.
            pass
    if not edited:
        try:
            await state.message.edit_text(text, reply_markup=_markup(state))
            state.rich = False
        except Exception:
            # Message deleted by the user (or flood): recreate it on
            # the next refresh instead of crashing the transfer.
            state.message = None
            state.rich = False
            return
    state._last_edit = time.time()
    state._last_text = text


async def _delayed_refresh(state: _ChatState, delay: float) -> None:
    await asyncio.sleep(delay)
    state._refresh_task = None
    await _refresh(state)


async def _refresh(state: _ChatState, force: bool = False) -> None:
    """Re-render the shared message, throttled to one edit per
    interval. A throttled refresh is retried once the interval
    passes, so the last update is never lost."""
    if not state.jobs:
        return
    async with state.lock:
        if state.message is None:
            if time.time() < state._broken_until:
                return
            state.message = await _send_shared(state)
            return
        now = time.time()
        if not force and now - state._last_edit < _INTERVAL:
            task = state._refresh_task
            if task is None or task.done():
                delay = _INTERVAL - (now - state._last_edit)
                state._refresh_task = asyncio.create_task(
                    _delayed_refresh(state, delay))
            return
        await _edit_now(state)


async def _repost(state: _ChatState) -> None:
    """Move the shared message to the bottom of the chat (v1.51.6):
    send a fresh one with the current jobs, then delete the old one.
    Send-first so a failed send keeps the old message on screen.
    A no-op when nothing is running, or when the last re-post was
    too recent (a multi-link burst registers jobs back to back) —
    the caller then edits in place instead."""
    if not state.jobs:
        return
    async with state.lock:
        if time.time() - state._last_send < _REPOST_MIN_INTERVAL:
            # Too soon to send again: edit in place instead. (Direct
            # _edit_now, not _refresh — the lock is already held here
            # and asyncio locks are not re-entrant.)
            await _edit_now(state)
            return
        task = state._refresh_task
        state._refresh_task = None
        if task is not None and not task.done():
            task.cancel()
        old, state.message = state.message, None
        new = await _send_shared(state)
        if new is None:
            # Send failed: keep the old message on screen.
            state.message = old
            return
        state.message = new
        if old is not None:
            try:
                await old.delete()
            except Exception:
                pass


async def _delete_shared(state: _ChatState) -> None:
    async with state.lock:
        msg, state.message = state.message, None
        state.rich = False
        task = state._refresh_task
        state._refresh_task = None
        if task is not None and not task.done():
            task.cancel()
        if msg is not None:
            try:
                await msg.delete()
            except Exception:
                pass
        # No jobs and no message: drop the chat's state entirely.
        _CHATS.pop(state.chat_id, None)
