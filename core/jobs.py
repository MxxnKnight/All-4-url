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
"""

import asyncio
import time

#: Minimum seconds between edits of the shared message (Telegram
#: punishes rapid edits; same 3.5 s as ThrottledEditor).
_INTERVAL = 3.5

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
        await self._remove()
        try:
            await self._state.client.send_message(self._state.chat_id,
                                                  text)
        except Exception:
            pass

    async def finish(self, text: str | None = None) -> None:
        """End the job quietly (or with a final notice as its own
        message when `text` is given). Idempotent."""
        await self._remove()
        if text:
            try:
                await self._state.client.send_message(self._state.chat_id,
                                                      text)
            except Exception:
                pass

    async def _remove(self) -> None:
        if self._gone:
            return
        self._gone = True
        state = self._state
        state.jobs.pop(self.id, None)
        if not state.jobs:
            # Last job gone: the shared message has nothing to show.
            await _delete_shared(state)
        else:
            await _refresh(state, force=True)


class _ChatState:
    """One chat's shared message + its jobs."""

    def __init__(self, client, chat_id: int):
        self.client = client
        self.chat_id = chat_id
        self.jobs: dict[str, Job] = {}
        self.message = None
        self.lock = asyncio.Lock()
        self._last_edit = 0.0
        self._last_text = ""
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


def _markup(state: _ChatState):
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    for job in state.jobs.values():
        label = job.label if len(job.label) <= 22 else job.label[:21] + "…"
        rows.append([InlineKeyboardButton(
            f"⏹ Stop {label}", callback_data=f"jb:stop:{job.id}")])
    return InlineKeyboardMarkup(rows) if rows else None


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
            try:
                state.message = await client.send_message(
                    chat_id, _render(state), reply_markup=_markup(state),
                    reply_to_message_id=getattr(reply_to, "id", None))
                state._last_edit = time.time()
                state._last_text = _render(state)
                return job
            except Exception:
                # Chat blocked / send failed: don't retry every edit.
                state._broken_until = time.time() + 60
                return job
    await _refresh(state, force=True)
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
    try:
        await state.message.edit_text(text, reply_markup=_markup(state))
    except Exception:
        # Message deleted by the user (or flood): recreate it on the
        # next refresh instead of crashing the transfer.
        state.message = None
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
            try:
                state.message = await state.client.send_message(
                    state.chat_id, _render(state),
                    reply_markup=_markup(state))
                state._last_edit = time.time()
                state._last_text = _render(state)
            except Exception:
                state._broken_until = time.time() + 60
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


async def _delete_shared(state: _ChatState) -> None:
    async with state.lock:
        msg, state.message = state.message, None
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
