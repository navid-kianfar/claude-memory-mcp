"""Compose-box attachments: copied out of the transcript, parked, bound on a yes.

A file the user drops into the Claude compose box has no path. It reaches the
model as an inline base64 `image` (or `document`) block inside a `type: "user"`
transcript entry, and the transcript JSONL at the hook's `transcript_path` is the
only place the bytes can be read back from. So the pipeline is:

    UserPromptSubmit / Stop hook
      -> POST /api/hook/attachments        (web/hooks.py, fails soft)
      -> handle_hook: confine the path, scan the new transcript bytes
      -> park: decode, cap, hash, copy into the task store, one inbox row
      -> a notice per parked file, delivered in the next UserPromptSubmit body
      -> the session asks the user, then memory_task_attach(pending_id=...)
      -> TaskService.attach -> outbox -> the flusher uploads it once

THE DAEMON NEVER BINDS ON ITS OWN. The user's decision (2026-09-13, "Ask before
attaching"): even with exactly one task in progress the file is parked and the
session is told to ask. `AUTO_BIND_SINGLE_CANDIDATE` is the one switch that would
change that, and it is off.

WHAT IS TRUSTED. Nothing in a hook payload. `transcript_path` is confined to
`~/.claude/projects/` before anything opens it, because what is read here ends up
uploaded to a remote board. Inside the file, only blocks in genuine user message
content qualify - a `tool_result` image is a screenshot the model took, and an
`isMeta` image is the Read tool's rendering of a PDF the model opened. Neither is
the user's, and neither is ever parked.

WHAT IS BOUNDED. A line longer than a maximum-size attachment can be is skipped
without being read into memory; `data` that is not canonical base64, or whose
decoded size is over the cap, is refused from its length before a byte is decoded;
a scan stops after `SCAN_BUDGET_SECONDS` and the next hook continues from there.
"""

import base64
import functools
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from memory_mcp.config import settings
from memory_mcp.db.registry import get_setting, set_setting
from memory_mcp.exceptions import MemoryMCPError
from memory_mcp.models import PendingAttachment, TaskAttachment

#: The user chose "Ask before attaching" on 2026-09-13. With this False the daemon
#: parks every file and, when exactly one task is in progress, tells the session
#: to ASK whether it belongs there. True would bind that single candidate on sight.
AUTO_BIND_SINGLE_CANDIDATE = False

#: How long one hook call may spend reading the transcript. What is left is read by
#: the next call, from the saved offset - the hook itself sits behind a timeout.
SCAN_BUDGET_SECONDS = 0.5

#: The most the response body carries. Undelivered notices beyond it wait a turn.
NOTICE_BODY_CAP = 2000

#: Room for the JSON entry around a maximum-size base64 payload.
LINE_ENVELOPE_BYTES = 1024 * 1024

#: Bytes that must still match for a resume to be trusted: this much just before
#: the saved offset (a whole entry's uuid and timestamp, on a real transcript),
#: and this much at the head of the file (its first entry, which a rotated or
#: rewritten transcript does not share).
_FINGERPRINT_TAIL_BYTES = 4096
_FINGERPRINT_HEAD_BYTES = 1024
_READ_CHUNK = 1024 * 1024
_OFFSET_KEY_PREFIX = "attach:offset:"
_TITLE_MAX = 80
_CANDIDATES_LISTED = 5

# Claude Code session ids are UUIDs. Anything else is not a session this daemon
# will key rows or registry settings on.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
# Canonical, unwrapped base64. Linear on its input: one greedy class, no nesting.
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]*={0,2}")
_MEDIA_TYPE_RE = re.compile(r"[a-z]+/[a-z0-9.+-]{1,64}")
_UNPRINTABLE_RE = re.compile(r"[\x00-\x1f\x7f\s]+")
_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "application/pdf": "pdf",
}


class PendingAttachmentNotFoundError(MemoryMCPError):
    """No parked file has this pending_id in this project."""


# ---------- the untrusted edge ----------


def claude_projects_root() -> Path:
    """Where Claude Code keeps transcripts. Resolved, so a comparison against a
    resolved candidate cannot be fooled by a symlinked home."""
    return (Path.home() / ".claude" / "projects").resolve()


def confine_transcript(raw: str) -> Path | None:
    """The transcript a hook named, or None when it must not be opened.

    Refused: empty or absurd input, a relative path, any `..` component, anything
    that is not a `.jsonl`, a symlink, a path whose real location is outside
    `~/.claude/projects/` (a symlinked parent escaping the tree included), a
    subagent's own transcript, and anything that is not a regular file -
    directories, FIFOs and devices. `open_transcript` re-checks the file type on
    the descriptor it actually reads, so a swap after this check gains nothing.
    """
    if not isinstance(raw, str) or not raw or len(raw) > 4096 or "\x00" in raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return None
    if candidate.suffix != ".jsonl":
        return None
    try:
        root = claude_projects_root()
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve(strict=True)
        if root not in resolved.parents:
            return None
        if "subagents" in resolved.relative_to(root).parts:
            return None
        info = resolved.stat()
        # A hard link is a second name for a file that may live anywhere; Claude
        # Code never writes a transcript as one.
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def open_transcript(path: Path):
    """Open without following a final symlink and without blocking on a FIFO,
    then insist the descriptor really is a regular file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {path.name}")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def user_supplied_blocks(entry) -> list[dict]:
    """The base64 image/document blocks the USER put in this entry, else [].

    Matched on the shapes measured across every local transcript (2.1.181 to
    2.1.260): a compose-box paste is `type: "user"` with `origin.kind == "human"`
    (or, on builds that predate `origin`, a `promptSource`), a `message.role` of
    user, and list content with no `tool_result`. Refused outright: sidechains,
    `isMeta` entries (the Read tool's PDF pages), entries carrying
    `toolUseResult`/`sourceToolAssistantUUID` (tool output), and every other
    origin (`task-notification`, `coordinator`, ...).
    """
    if not isinstance(entry, dict) or entry.get("type") != "user":
        return []
    if entry.get("isSidechain") or entry.get("isMeta"):
        return []
    if "toolUseResult" in entry or "sourceToolAssistantUUID" in entry:
        return []
    origin = entry.get("origin")
    if origin is not None:
        if not isinstance(origin, dict) or origin.get("kind") != "human":
            return []
    elif not isinstance(entry.get("promptSource"), str):
        return []
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return []
    return [
        b for b in content
        if isinstance(b, dict)
        and b.get("type") in ("image", "document")
        and isinstance(b.get("source"), dict)
        and b["source"].get("type") == "base64"
    ]


def decoded_size(data: str, cap: int) -> int | None:
    """The exact decoded length of canonical base64, or None when it is not
    canonical or would decode to more than `cap` bytes.

    Decided from the length and the padding alone, so an oversized or malformed
    payload is refused without allocating its decoded form.
    """
    n = len(data)
    if n == 0 or n % 4 or n > (cap + 2) // 3 * 4:
        return None
    if not _BASE64_RE.fullmatch(data):
        return None
    padding = 2 if data.endswith("==") else 1 if data.endswith("=") else 0
    size = n // 4 * 3 - padding
    return size if 0 < size <= cap else None


def _clean(text: str | None, limit: int = _TITLE_MAX) -> str:
    """A task title as it may appear inside a one-line notice: no newlines or
    control characters, no double quotes to break the quoting, bounded."""
    flat = _UNPRINTABLE_RE.sub(" ", text or "").replace('"', "'").strip()
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def _filename(stamp: datetime, n: int, media_type: str) -> str:
    ext = _EXTENSIONS.get(media_type, "bin")
    return f"attachment-{stamp.strftime('%Y%m%d-%H%M%S')}-{n}.{ext}"


def _entry_stamp(entry: dict) -> datetime:
    """The entry's own timestamp, so a re-scan names the file the same way."""
    raw = entry.get("timestamp")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _fingerprint(fh, offset: int) -> str:
    """What the bytes up to `offset` looked like, cheaply: the head of the file
    and the stretch just before the offset, never more than a few KiB."""
    fd = fh.fileno()
    digest = hashlib.sha1()
    digest.update(os.pread(fd, min(offset, _FINGERPRINT_HEAD_BYTES), 0))
    start = max(0, offset - _FINGERPRINT_TAIL_BYTES)
    digest.update(os.pread(fd, offset - start, start))
    return digest.hexdigest()


# ---------- the service ----------


class AttachmentInboxService:
    """Park compose-box files and bind them to tasks when a session says so."""

    def __init__(self, inbox_repo, session_repo, task_service):
        self._inbox = inbox_repo
        self._sessions = session_repo
        self._tasks = task_service
        self._scan_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ---------- the hook ----------

    def handle_hook(
        self, project: str, claude_session_id: str, transcript_path: str,
        *, deliver: bool,
    ) -> str:
        """Scan what is new in the transcript, park what the user attached, and
        answer with this session's undelivered notices, one per line.

        `deliver` is True only for UserPromptSubmit: its stdout is added to the
        model's context, Stop's is not, so a notice answered to Stop stays
        undelivered and goes out with the next prompt.
        """
        if settings.server_mode:
            # The transcript lives on the client's machine, not the server's, and
            # a shared server's own home is nobody's to read on a user's behalf.
            return ""
        if not isinstance(claude_session_id, str) or not _SESSION_ID_RE.fullmatch(
            claude_session_id
        ):
            return ""
        transcript = confine_transcript(transcript_path)
        if transcript is None:
            return ""
        self.scan_transcript(project, claude_session_id, transcript)
        return self.undelivered_notices(project, claude_session_id, deliver=deliver)

    # ---------- scanning ----------

    def _scan_lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._scan_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._scan_locks[key] = lock
            return lock

    @staticmethod
    def _offset_key(claude_session_id: str, transcript: Path) -> str:
        digest = hashlib.sha1(str(transcript).encode()).hexdigest()
        return f"{_OFFSET_KEY_PREFIX}{claude_session_id}:{digest}"

    def _max_line_bytes(self) -> int:
        cap = self._tasks.MAX_ATTACHMENT_BYTES
        return (cap + 2) // 3 * 4 + LINE_ENVELOPE_BYTES

    def scan_transcript(
        self, project: str, claude_session_id: str, transcript: Path,
        *, clock=time.monotonic,
    ) -> list[PendingAttachment]:
        """Park every user-supplied file in the transcript bytes not yet read.

        Resumes from the saved offset when the file is still the one it was
        taken from - same inode, not shorter, and its head and the bytes just
        before the offset unchanged. Anything else (rotated, truncated, rewritten, recreated)
        starts over from the tail; the (session, sha256) key makes that re-read
        park nothing twice. The first read of a transcript starts one
        maximum-size line from its end, so the newest paste always fits whole
        and a long-lived session costs bounded work.

        A call that finds another scan of the same transcript running returns at
        once rather than racing it.
        """
        key = self._offset_key(claude_session_id, transcript)
        lock = self._scan_lock(key)
        if not lock.acquire(blocking=False):
            return []
        try:
            with open_transcript(transcript) as fh:
                return self._scan(project, claude_session_id, fh, key, clock)
        except OSError:
            return []
        finally:
            lock.release()

    def _resume_point(self, fh, st: os.stat_result, key: str) -> int | None:
        try:
            saved = json.loads(get_setting(key) or "null")
        except Exception:  # noqa: BLE001 - unreadable offset: re-read the tail, the inbox dedupes
            return None
        if not isinstance(saved, dict):
            return None
        offset = saved.get("offset")
        if not isinstance(offset, int) or offset < 0 or offset > st.st_size:
            return None
        if saved.get("ino") != st.st_ino or saved.get("dev") != st.st_dev:
            return None
        if saved.get("fp") != _fingerprint(fh, offset):
            return None
        return offset

    def _scan(self, project, claude_session_id, fh, key, clock) -> list[PendingAttachment]:
        st = os.fstat(fh.fileno())
        size = st.st_size
        max_line = self._max_line_bytes()
        start = self._resume_point(fh, st, key)
        fresh = start is None
        if fresh:
            start = max(0, size - max_line)
        fh.seek(start)
        pos = start
        if fresh and start > 0:
            # Landed inside a line: drop the partial one, unread.
            skipped, complete = self._skip_line(fh)
            if not complete:
                return []
            pos += skipped

        parked: list[PendingAttachment] = []
        # Read once per scan and only if something is parked: most turns attach
        # nothing, and they should cost no task query.
        candidates = functools.cache(lambda: self.candidates(project))
        parsed_any = False
        deadline = clock() + SCAN_BUDGET_SECONDS
        # The budget is checked AFTER each line, so every call makes progress: a
        # first park that is slow on its own (a cold connection) must not leave
        # the offset where it was forever.
        while pos < size:
            if parsed_any and clock() >= deadline:
                break
            parsed_any = True
            line, consumed, complete = self._read_line(fh, max_line)
            if not complete:
                break  # a line still being written is left for the next call
            pos += consumed
            if line is None or b'"base64"' not in line:
                continue
            try:
                entry = json.loads(line)
                parked.extend(
                    self._park_entry(project, claude_session_id, entry, candidates)
                )
            except Exception:  # noqa: BLE001 - a line that breaks costs that line only
                continue

        try:
            set_setting(key, json.dumps({
                "offset": pos, "ino": st.st_ino, "dev": st.st_dev,
                "fp": _fingerprint(fh, pos),
            }))
        except Exception:  # noqa: BLE001 - a lost offset re-reads; the inbox dedupes
            pass
        return parked

    @staticmethod
    def _skip_line(fh) -> tuple[int, bool]:
        """Consume through the next newline without holding the line."""
        consumed = 0
        while True:
            chunk = fh.readline(_READ_CHUNK)
            if not chunk:
                return consumed, False
            consumed += len(chunk)
            if chunk.endswith(b"\n"):
                return consumed, True

    @classmethod
    def _read_line(cls, fh, limit: int) -> tuple[bytes | None, int, bool]:
        """`(line, bytes consumed, newline reached)`.

        `line` is None for a line over `limit`, which is consumed without being
        held - it cannot carry an attachment this service would accept.
        """
        head = fh.readline(limit + 1)
        if not head:
            return None, 0, False
        if head.endswith(b"\n"):
            return head, len(head), True
        if len(head) <= limit:
            return None, 0, False  # end of file mid-line
        consumed = len(head)
        del head
        rest, complete = cls._skip_line(fh)
        return None, consumed + rest, complete

    def _park_entry(
        self, project: str, claude_session_id: str, entry, candidates,
    ) -> list[PendingAttachment]:
        parked = []
        blocks = user_supplied_blocks(entry)
        if not blocks:
            return parked
        stamp = _entry_stamp(entry)
        for n, block in enumerate(blocks, start=1):
            try:
                pending = self._park_block(
                    project, claude_session_id, block, stamp, n, candidates,
                )
            except Exception:  # noqa: BLE001 - one bad block costs that block only
                continue
            if pending is not None:
                parked.append(pending)
        return parked

    def _park_block(
        self, project: str, claude_session_id: str, block: dict,
        stamp: datetime, n: int, candidates,
    ) -> PendingAttachment | None:
        source = block["source"]
        data = source.get("data")
        if not isinstance(data, str):
            return None
        cap = self._tasks.MAX_ATTACHMENT_BYTES
        size = decoded_size(data, cap)
        if size is None:
            return None
        raw = base64.b64decode(data, validate=True)
        if len(raw) != size or not 0 < len(raw) <= cap:
            return None
        media_type = source.get("media_type")
        if not isinstance(media_type, str) or not _MEDIA_TYPE_RE.fullmatch(media_type):
            media_type = "application/octet-stream"
        return self.park(
            project, claude_session_id, raw,
            filename=_filename(stamp, n, media_type), content_type=media_type,
            candidates=candidates,
        )

    # ---------- the inbox ----------

    def park(
        self, project: str, claude_session_id: str, content: bytes,
        *, filename: str, content_type: str | None, source: str = "transcript",
        candidates=None,
    ) -> PendingAttachment | None:
        """Copy bytes into the task store and record them as waiting for a task.

        Returns None when this session already parked these bytes, or when they
        are empty or over the attachment limit - refused before anything is
        written. The notice is decided here and inserted WITH the row: a row
        whose notice was a second write that failed would be a file parked
        without anyone being told, and the dedupe would keep it that way.

        `candidates` is a zero-argument callable, so one scan reads the task
        list once however many files it parks.
        """
        size = len(content)
        if size == 0 or size > self._tasks.MAX_ATTACHMENT_BYTES:
            return None
        sha = hashlib.sha256(content).hexdigest()
        if self._inbox.find(project, claude_session_id, sha) is not None:
            return None
        try:
            found = candidates() if candidates is not None else self.candidates(project)
        except Exception:  # noqa: BLE001 - unknown reads as none: "start or pick a task"
            found = []
        blob = self._store(project, sha, content)
        pending = PendingAttachment(
            id=str(uuid.uuid4()), claude_session_id=claude_session_id, sha256=sha,
            filename=filename, content_type=content_type, size_bytes=size,
            source=source,
        )
        pending.notice = self.notice_for(pending, found)
        self._inbox.add(project, pending, str(blob))
        if AUTO_BIND_SINGLE_CANDIDATE and len(found) == 1:
            self._auto_bind(project, pending, found[0])
        return pending

    @staticmethod
    def _store(project: str, sha: str, content: bytes) -> Path:
        """The content-addressed store `TaskService.attach` reads and writes, so a
        later bind finds the blob already there and copies nothing. Written to a
        temporary name and renamed, so a half-written blob is never visible."""
        store = Path(settings.data_dir) / "attachments" / project / sha[:2]
        store.mkdir(parents=True, exist_ok=True)
        blob = store / sha
        if blob.exists():
            return blob
        partial = store / f".{sha}.{uuid.uuid4().hex}.part"
        try:
            with open(partial, "xb") as out:
                out.write(content)
            os.replace(partial, blob)
        finally:
            partial.unlink(missing_ok=True)
        return blob

    def candidates(self, project: str) -> list[dict]:
        """In-progress tasks on a live claim with a clock running, held by a LEAD
        session. A dispatched agent's task is not where the user's paste goes; a
        session that named an agent at `memory_session_start` is excluded."""
        leads = set(self._sessions.open_lead_sessions(project))
        found: dict[str, dict] = {}
        for row in self._inbox.candidates(project):
            if row["clock_session"] in leads and row["id"] not in found:
                found[row["id"]] = row
        return list(found.values())

    def _auto_bind(self, project: str, pending: PendingAttachment, only: dict) -> None:
        """Only when AUTO_BIND_SINGLE_CANDIDATE is switched on. A failed bind keeps
        the ask notice already stored, so the session is still told."""
        try:
            attachment = self.bind(project, pending.id, only["id"])
        except Exception:  # noqa: BLE001
            return
        pending.notice = (
            f"[Memory MCP] The user attached {pending.filename}; it is now on "
            f'"{_clean(only["title"])}" ({only["id"]}). If that is wrong: '
            f'memory_task_detach(attachment_id="{attachment.id}").'
        )
        self._inbox.set_notice(project, pending.id, pending.notice)

    @staticmethod
    def notice_for(pending: PendingAttachment, candidates: list[dict]) -> str:
        """The one line the session is told about a parked file."""
        head = (
            f"[Memory MCP] The user attached {pending.filename}; "
            f"it is parked as {pending.id}."
        )
        bind = f'memory_task_attach(task_id=..., pending_id="{pending.id}")'
        if not candidates:
            return (
                f"{head} No task is in progress to put it on: start or pick the "
                f"task it belongs to, then {bind}."
            )
        if len(candidates) == 1:
            only = candidates[0]
            return (
                f'{head} Ask the user whether it belongs on the task in progress, '
                f'"{_clean(only["title"])}" ({only["id"]}); only on a yes, '
                f'memory_task_attach(task_id="{only["id"]}", pending_id="{pending.id}").'
            )
        listed = "; ".join(
            f'"{_clean(c["title"])}" ({c["id"]})' for c in candidates[:_CANDIDATES_LISTED]
        )
        more = len(candidates) - _CANDIDATES_LISTED
        if more > 0:
            listed += f"; and {more} more"
        return (
            f"{head} Several tasks are in progress: {listed}. Bind it to the one "
            f"you are on with {bind}."
        )

    def undelivered_notices(
        self, project: str, claude_session_id: str, *, deliver: bool,
    ) -> str:
        """This session's notices not yet in its context, capped at
        NOTICE_BODY_CAP. Only what fits is marked delivered, and only if asked."""
        lines: list[str] = []
        ids: list[str] = []
        used = 0
        for pending_id, notice in self._inbox.undelivered(project, claude_session_id):
            cost = len(notice) + (1 if lines else 0)
            if used + cost > NOTICE_BODY_CAP:
                if lines:
                    break
                notice = notice[:NOTICE_BODY_CAP]
                cost = len(notice)
            lines.append(notice)
            ids.append(pending_id)
            used += cost
        if deliver and ids:
            self._inbox.mark_notified(project, ids)
        return "\n".join(lines)

    # ---------- binding ----------

    def get(self, project: str, pending_id: str) -> PendingAttachment:
        found = self._inbox.get(project, pending_id)
        if found is None:
            raise PendingAttachmentNotFoundError(f"No parked attachment: {pending_id}")
        return found[0]

    def bind(
        self, project: str, pending_id: str, task_id: str, filename: str | None = None,
    ) -> TaskAttachment:
        """Attach a parked file to a task through `TaskService.attach`.

        One code path for every attachment, so every guard applies - the task
        must exist, the (task, bytes) pair is attached once - and the blob is
        already in the store, so nothing is copied. Binding the same pending_id
        to the same task again returns the same attachment.
        """
        found = self._inbox.get(project, pending_id)
        if found is None:
            raise PendingAttachmentNotFoundError(f"No parked attachment: {pending_id}")
        pending, path = found
        if not Path(path).is_file():
            raise MemoryMCPError(
                f"the file parked as {pending_id} is no longer in the task store; "
                "ask the user to attach it again"
            )
        attachment = self._tasks.attach(
            project, task_id, path,
            filename=filename or pending.filename, content_type=pending.content_type,
        )
        self._inbox.mark_bound(project, pending_id, task_id)
        return attachment
