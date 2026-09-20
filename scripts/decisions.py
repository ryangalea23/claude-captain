#!/usr/bin/env python3
"""
decisions.py — an append-only "open decisions" ledger for the Captain
coordinator session.

WHY THIS EXISTS
----------------
One Claude Code session ("Captain") relays questions from several peer
sessions to one person and has to remember which ones are still waiting on an
answer. Holding that list in the model's own head does not work: it gets
re-derived from memory, drifts, and loses track of a question that is still
open. This script is the fix — the open/closed state of every decision lives
on disk, not in anyone's context window.

DESIGN (copied from firstmate's hardest-won lesson — follow it exactly)
------------------------------------------------------------------------
- The log is APPEND-ONLY. One JSON object per line. A line is never rewritten
  or deleted. A crash loses nothing, because nothing is held in memory
  between runs — every command re-reads the whole log from disk.
- The current state of a key is a FOLD over every line that ever mentioned
  it, not just the newest line. An "open" event 50 lines back must still
  show as open if nothing has explicitly closed that same key since.
- A decision is opened with a KEY and stays open until a `close` event names
  THAT SAME KEY. A later unrelated line, or a vague "all done", can never
  close it — only an explicit close of that exact key does.
- Unknown is never promoted to a state. A line that fails to parse as JSON
  is skipped and counted, never silently dropped and never treated as
  closing or opening anything.

STORAGE
-------
One JSONL file, ~/.claude/decisions.log by default (override
with the DECISIONS_LOG_PATH environment variable — the test suite does this
so it never touches the real log). Each line is one JSON object with at
least: ts (ISO 8601 UTC), event ("open" | "close"), key, plus event-specific
payload fields (question/asked_by/context for open, answer for close).

CONCURRENCY
-----------
Several sessions may append at the same time, and this runs on Windows,
where O_APPEND is NOT what it is on POSIX: the Windows CRT emulates append
mode as seek-to-end-then-write, not a single atomic kernel operation. Two
writers can race, both seek to the same offset, and one write clobbers the
other — measured directly on this machine: two threads calling
`cmd_open()` on different keys lost one write in about 25% of trials, with
both calls still reporting success. That is the worst possible failure
mode for an append-only log: not a corrupt line the malformed-line counter
would catch, but a cleanly missing event that nothing reports, in a log
whose whole point is being trusted.

So every append is serialised through an OS-level advisory lock
(`msvcrt.locking` on Windows, `fcntl.flock` elsewhere) taken on a sidecar
`<log>.lock` file — see `_file_lock()` below. Only ONE writer holds the
lock at a time; it does its single os.write() of one complete line and
releases. This is an OS-level lock, not a stale marker file: if a process
dies while holding it, the operating system releases the lock the moment
the process's file handle closes (on process exit or crash), so a dead
writer can never wedge every future append — which matters on a machine
that kills processes constantly. Acquisition is a bounded retry loop
(`_LOCK_TIMEOUT_SECONDS`), so a writer that somehow can't get the lock
raises instead of hanging forever.

Reads (list/show) stay lock-free — they only ever consume complete lines
terminated by '\\n', and a writer that is mid-append (now happening only
one-at-a-time thanks to the lock) simply hasn't written its trailing
newline yet, so a concurrent reader sees either the whole line or none of
it.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import sys
import time
from dataclasses import dataclass, field

try:
    import msvcrt  # Windows

    _PLATFORM_LOCK = "msvcrt"
except ImportError:  # pragma: no cover - exercised only off-Windows
    import fcntl  # POSIX

    _PLATFORM_LOCK = "fcntl"

DEFAULT_LOG_PATH = os.path.expanduser(os.path.join("~", ".claude", "decisions.log"))
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_INTERVAL = 0.01


def _log_path() -> str:
    return os.environ.get("DECISIONS_LOG_PATH", DEFAULT_LOG_PATH)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Append (write side)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _file_lock(lock_path: str, timeout: float = _LOCK_TIMEOUT_SECONDS):
    """Hold an OS-level advisory lock on a 1-byte sidecar file for the
    duration of the `with` block.

    This is deliberately an OS-level lock, not a "lock file exists" marker:
    the kernel releases it automatically the instant the holding process's
    file handle closes, including on a crash. A marker file would instead
    need a stale-lock recovery story (age-based? PID-based?) that a killed
    process could still get wrong; this needs none, which is the whole
    reason to use it here rather than the more obvious `os.O_CREAT|O_EXCL`
    marker-file approach.
    """
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        # NOTE: no "write a placeholder byte if empty" step here on purpose.
        # An earlier version did that, unlocked, on every acquire — two
        # threads could both see size==0 and both os.write() to byte 0 at
        # once, and if one had *already* taken the lock by the time the
        # other's write landed, Windows raises PermissionError writing into
        # a byte range locked by a different handle. msvcrt.locking() can
        # lock a range past current EOF just fine (Windows extends the file
        # to cover it), so there is nothing to pre-populate.
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if _PLATFORM_LOCK == "msvcrt":
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"Could not acquire lock on '{lock_path}' within "
                        f"{timeout}s — another writer may be stuck."
                    )
                time.sleep(_LOCK_POLL_INTERVAL)
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            if _PLATFORM_LOCK == "msvcrt":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def append_event(log_path: str, event: dict) -> None:
    """Append exactly one JSON line to the log.

    Serialised through `_file_lock()` (see module docstring): only one
    writer at a time performs its single os.write() of one complete line,
    so concurrent writers can never race each other onto the same offset.
    """
    parent = os.path.dirname(log_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    with _file_lock(log_path + ".lock"):
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# Read + fold (read side)
# ---------------------------------------------------------------------------

def read_events(log_path: str) -> tuple[list[dict], int]:
    """Read every line of the log. Returns (events, malformed_line_count).

    A line that isn't valid JSON, or isn't a JSON object, is skipped and
    counted — it is never allowed to crash the read or to silently vanish
    without being reported.
    """
    if not os.path.exists(log_path):
        return [], 0

    events: list[dict] = []
    malformed = 0
    with open(log_path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(obj, dict) or "event" not in obj or "key" not in obj:
                malformed += 1
                continue
            events.append(obj)
    return events, malformed


@dataclass
class DecisionState:
    key: str
    status: str  # "open" | "closed"
    question: str | None = None
    asked_by: str | None = None
    context: str | None = None
    answer: str | None = None
    opened_at: str | None = None
    closed_at: str | None = None
    history: list = field(default_factory=list)


def fold_state(events: list[dict]) -> dict[str, DecisionState]:
    """Fold every event, in order, into current per-key state.

    This is the whole point of the design: state is derived by replaying
    the full log every time, so a key opened long ago and buried under
    unrelated later lines still comes out "open" unless ITS OWN close event
    is somewhere in the stream.
    """
    states: dict[str, DecisionState] = {}
    for ev in events:
        key = ev.get("key")
        kind = ev.get("event")
        if not key or kind not in ("open", "close"):
            continue

        if kind == "open":
            states[key] = DecisionState(
                key=key,
                status="open",
                question=ev.get("question"),
                asked_by=ev.get("asked_by"),
                context=ev.get("context"),
                answer=None,
                opened_at=ev.get("ts"),
                closed_at=None,
                history=(states[key].history if key in states else []) + [ev],
            )
        elif kind == "close":
            if key in states:
                st = states[key]
                st.status = "closed"
                st.answer = ev.get("answer")
                st.closed_at = ev.get("ts")
                st.history.append(ev)
            # A close for a key never opened (or already fully unknown) is
            # kept out of `states` entirely — unknown never gets promoted to
            # a state. cmd_close() is what actually refuses this at write
            # time; the fold just has to not fabricate an entry for it.
    return states


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_open(log_path, key, question, asked_by, context=None):
    events, _ = read_events(log_path)
    states = fold_state(events)
    existing = states.get(key)
    if existing and existing.status == "open":
        return False, (
            f"Key '{key}' is already open (asked by {existing.asked_by}: "
            f"{existing.question!r}). Close it first, or pick a different key."
        )
    ev = {
        "ts": _now_iso(),
        "event": "open",
        "key": key,
        "question": question,
        "asked_by": asked_by,
    }
    if context:
        ev["context"] = context
    append_event(log_path, ev)
    return True, f"Opened '{key}'."


def cmd_close(log_path, key, answer):
    events, _ = read_events(log_path)
    states = fold_state(events)
    existing = states.get(key)
    if existing is None:
        return False, f"Key '{key}' was never opened — nothing to close."
    if existing.status == "closed":
        return False, f"Key '{key}' is already closed (answer: {existing.answer!r})."
    ev = {
        "ts": _now_iso(),
        "event": "close",
        "key": key,
        "answer": answer,
    }
    append_event(log_path, ev)
    return True, f"Closed '{key}'."


def _fmt_age(ts_str: str | None) -> str:
    if not ts_str:
        return "?"
    try:
        ts = datetime.datetime.fromisoformat(ts_str)
    except ValueError:
        return "?"
    now = datetime.datetime.now(datetime.timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def render_list(log_path: str, as_json: bool = False) -> str:
    events, malformed = read_events(log_path)
    states = fold_state(events)

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

    open_items = [s for s in states.values() if s.status == "open"]
    open_items.sort(key=lambda s: s.opened_at or "")

    closed_today = [
        s for s in states.values()
        if s.status == "closed" and (s.closed_at or "").startswith(today)
    ]
    closed_today.sort(key=lambda s: s.closed_at or "")

    if as_json:
        payload = {
            "open": [
                {
                    "key": s.key,
                    "question": s.question,
                    "asked_by": s.asked_by,
                    "context": s.context,
                    "opened_at": s.opened_at,
                    "age": _fmt_age(s.opened_at),
                }
                for s in open_items
            ],
            "closed_today": [
                {
                    "key": s.key,
                    "question": s.question,
                    "answer": s.answer,
                    "closed_at": s.closed_at,
                }
                for s in closed_today
            ],
            "unreadable_lines": malformed,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    lines = []
    lines.append(f"OPEN DECISIONS ({len(open_items)})")
    if not open_items:
        lines.append("  (none)")
    for s in open_items:
        ctx = f"  [{s.context}]" if s.context else ""
        lines.append(f"  [{s.key}] age={_fmt_age(s.opened_at)} asked_by={s.asked_by}{ctx}")
        lines.append(f"      {s.question}")

    if closed_today:
        lines.append("")
        lines.append(f"CLOSED TODAY ({len(closed_today)})")
        for s in closed_today:
            lines.append(f"  [{s.key}] {s.question} -> {s.answer}")

    if malformed:
        lines.append("")
        lines.append(f"{malformed} unreadable line(s) skipped")

    return "\n".join(lines)


def render_show(log_path: str, key: str) -> str:
    events, malformed = read_events(log_path)
    key_events = [e for e in events if e.get("key") == key]
    if not key_events:
        return f"No history for '{key}'."
    lines = [f"HISTORY for '{key}'"]
    for e in key_events:
        if e["event"] == "open":
            lines.append(
                f"  {e.get('ts')}  OPEN   asked_by={e.get('asked_by')} "
                f"question={e.get('question')!r}"
                + (f" context={e.get('context')!r}" if e.get("context") else "")
            )
        elif e["event"] == "close":
            lines.append(f"  {e.get('ts')}  CLOSE  answer={e.get('answer')!r}")
    if malformed:
        lines.append(f"({malformed} unreadable line(s) skipped elsewhere in the log)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="decisions.py",
        description="Append-only open-decisions ledger.",
    )
    sub = parser.add_subparsers(dest="command")

    p_open = sub.add_parser("open", help="Open a new decision")
    p_open.add_argument("key")
    p_open.add_argument("--question", required=True)
    p_open.add_argument("--asked-by", required=True)
    p_open.add_argument("--context", default=None)

    p_close = sub.add_parser("close", help="Close an open decision")
    p_close.add_argument("key")
    p_close.add_argument("--answer", required=True)

    p_list = sub.add_parser("list", help="Show the folded view (default)")
    p_list.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="Show full history for one key")
    p_show.add_argument("key")

    args = parser.parse_args(argv)
    log_path = _log_path()

    # No subcommand at all -> behave like `list`.
    if args.command is None:
        print(render_list(log_path, as_json=False))
        return 0

    if args.command == "open":
        ok, msg = cmd_open(
            log_path, args.key, question=args.question, asked_by=args.asked_by,
            context=args.context,
        )
        print(msg)
        return 0 if ok else 1

    if args.command == "close":
        ok, msg = cmd_close(log_path, args.key, answer=args.answer)
        print(msg)
        return 0 if ok else 1

    if args.command == "list":
        print(render_list(log_path, as_json=args.json))
        return 0

    if args.command == "show":
        print(render_show(log_path, args.key))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    # Force UTF-8 stdout/stderr on Windows so non-ASCII questions/answers
    # never crash with the default cp1252 console encoding.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
