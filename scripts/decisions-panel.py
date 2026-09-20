#!/usr/bin/env python3
"""
decisions-panel.py — a live, full-screen terminal panel over the open-
decisions ledger (decisions.py / decisions.log).

WHY THIS EXISTS
----------------
`decisions.py list` is a one-shot snapshot: you have to remember to re-run it
to see what changed. This panel stays open, refreshes itself the moment
another process (the Captain coordinator, a peer session) opens or closes a
decision, and lets you arrow-key to the oldest-waiting question and close it
right there.

REUSE, NOT REIMPLEMENTATION
----------------------------
All log parsing comes from `decisions.py`: `read_events`, `fold_state`, and
`cmd_close` (which goes through the same OS-level file lock as the CLI). This
file adds ONLY the terminal UI and the live-refresh polling loop. decisions.py
itself needed no changes — its module-level code already guards the CLI
entrypoint behind `if __name__ == "__main__"`, so importing it here runs no
side effects.

LIVE REFRESH — WHY STAT, NOT RE-READ-AND-FOLD EVERY TICK
-----------------------------------------------------------
Every tick we call `os.stat()` on the log file and compare (size, mtime_ns)
to what we saw last tick. That is one syscall, regardless of how long the
log has grown to. Only when that tuple changes do we pay for
`read_events()` + `fold_state()` (which reads and re-parses the WHOLE file
every time, by decisions.py's own design — see its docstring). the author asked
for "within about a second" of another process's append; polling every
0.25s and reloading only on a real change gets there four times over per
second, while an idle panel does nothing more than one cheap stat() per
poll — no re-reading, no re-folding, no flicker.

TERMINAL SAFETY
----------------
Enters the alternate screen buffer and hides the cursor on start; a
`try/finally` (covering the normal quit path, Ctrl-C, and any unhandled
exception) always restores the cursor and leaves the alternate screen
buffer before the process exits, so a crash never leaves your shell in a
blank or cursor-less state. Windows consoles do not enable ANSI escape
processing by default outside recent Windows Terminal/Tabby profiles, so we
explicitly turn on `ENABLE_VIRTUAL_TERMINAL_PROCESSING` via the Win32 API on
startup rather than assume it.

KEYS
----
`curses` is not available on stock Windows Python, so keys come from
`msvcrt.getwch()`. Arrow keys arrive as a two-character sequence: a prefix
byte (`\xe0` or `\x00`) followed by a scan code (H=Up, P=Down, K=Left,
M=Right as their `chr()` forms below). Enter is `\r`. `q` quits, `c` closes
the selected/viewed decision (prompting for the answer text and writing it
through `decisions.cmd_close`, i.e. through the SAME lock as the CLI), `r`
forces an immediate reload, Escape or Left goes back from the detail view.
"""
from __future__ import annotations

import ctypes
import datetime
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decisions as dec  # noqa: E402 — reuse read_events/fold_state/cmd_close

try:
    import msvcrt
except ImportError:  # pragma: no cover — this panel is Windows-only by design
    msvcrt = None

POLL_SECONDS = 0.25  # cheap stat() cadence; see module docstring for why this
                      # comfortably beats the "within about a second" ask
                      # without re-reading the log on every tick

ESC = "\x1b"
ARROW_PREFIXES = ("\xe0", "\x00")
ARROW_UP, ARROW_DOWN, ARROW_LEFT, ARROW_RIGHT = "H", "P", "K", "M"


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without a real console — see test file)
# ---------------------------------------------------------------------------

def stat_key(log_path: str):
    """(size, mtime_ns) fingerprint of the log file, or None if it doesn't
    exist yet. Two of these compare unequal iff the file's content or
    mtime actually changed — the only signal the poll loop needs."""
    try:
        st = os.stat(log_path)
        return (st.st_size, st.st_mtime_ns)
    except FileNotFoundError:
        return None


def has_changed(old, new) -> bool:
    return old != new


def sort_open_items(states: dict) -> list:
    """Oldest-opened first — the one waiting longest is the one being
    forgotten, so it belongs at the top."""
    items = [s for s in states.values() if s.status == "open"]
    items.sort(key=lambda s: s.opened_at or "")
    return items


def move_selection(selected: int, delta: int, count: int) -> int:
    """Wrap the highlighted row at either end. Pure arithmetic so it can be
    unit-tested without any terminal at all."""
    if count <= 0:
        return 0
    return (selected + delta) % count


def clamp_selection(selected: int, count: int) -> int:
    if count <= 0:
        return 0
    return min(selected, count - 1)


# ---------------------------------------------------------------------------
# Terminal control
# ---------------------------------------------------------------------------

def _enable_windows_ansi() -> None:
    if os.name != "nt":
        return
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    kernel32 = ctypes.windll.kernel32
    for handle_id in (-11,):  # STD_OUTPUT_HANDLE
        handle = kernel32.GetStdHandle(handle_id)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)


def _enter_screen() -> None:
    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    sys.stdout.flush()


def _leave_screen() -> None:
    # Always show the cursor and drop back to the normal buffer, even mid-crash.
    sys.stdout.write("\x1b[?25h\x1b[?1049l")
    sys.stdout.flush()


def _term_size() -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=(100, 30))
    return size.columns, size.lines


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

FOOTER = "↑/↓ move   Enter detail   c close   r refresh   q quit"
FOOTER_DETAIL = "Esc/← back   c close   r refresh   q quit"


def _truncate(text: str, width: int) -> str:
    if text is None:
        return ""
    text = text.replace("\n", " ")
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def render_list(open_items: list, selected: int, malformed: int, log_path: str) -> str:
    cols, rows = _term_size()
    lines = []
    lines.append("\x1b[1mOPEN DECISIONS\x1b[0m" + f"  ({len(open_items)} open)")
    lines.append("")

    if not os.path.exists(log_path):
        lines.append("  no open decisions (log does not exist yet)")
    elif not open_items:
        lines.append("  no open decisions")
    else:
        for i, s in enumerate(open_items):
            age = dec._fmt_age(s.opened_at)
            asked_by = s.asked_by or "?"
            header = f" [{s.key}]  age={age}  asked_by={asked_by}"
            question = _truncate(s.question or "", max(10, cols - 6))
            if i == selected:
                lines.append(f"\x1b[7m{header}\x1b[0m")
                lines.append(f"\x1b[7m    {question}\x1b[0m")
            else:
                lines.append(header)
                lines.append(f"    {question}")

    body_budget = max(0, rows - len(lines) - 3)
    lines = lines[: len(lines)]  # (scrolling omitted — panel is meant for a
                                  # single terminal-height backlog; if it ever
                                  # overflows, the footer still stays pinned)

    footer_lines = [
        "",
        f"unreadable lines skipped: {malformed}",
        FOOTER,
    ]
    return "\n".join(lines[: rows - len(footer_lines) - 1] + footer_lines)


def render_detail(state, log_path: str) -> str:
    lines = []
    lines.append("\x1b[1mDECISION DETAIL\x1b[0m")
    lines.append("")
    lines.append(f"key:       {state.key}")
    lines.append(f"status:    {state.status}")
    lines.append(f"asked_by:  {state.asked_by}")
    lines.append(f"opened_at: {state.opened_at}  (age {dec._fmt_age(state.opened_at)})")
    if state.context:
        lines.append(f"context:   {state.context}")
    lines.append("")
    lines.append("question:")
    lines.append(f"  {state.question}")
    if state.status == "closed":
        lines.append("")
        lines.append(f"answer:    {state.answer}")
        lines.append(f"closed_at: {state.closed_at}")
    lines.append("")
    lines.append("history:")
    for ev in state.history:
        kind = ev.get("event")
        ts = ev.get("ts")
        if kind == "open":
            lines.append(f"  {ts}  OPEN   {ev.get('question')!r}")
        elif kind == "close":
            lines.append(f"  {ts}  CLOSE  {ev.get('answer')!r}")
    lines.append("")
    lines.append(FOOTER_DETAIL)
    return "\n".join(lines)


def _draw(text: str) -> None:
    sys.stdout.write("\x1b[H\x1b[2J")
    sys.stdout.write(text)
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Close prompt (drops out of the raw key loop just for this one line of input)
# ---------------------------------------------------------------------------

def prompt_close(log_path: str, key: str, question: str):
    cols, rows = _term_size()
    sys.stdout.write("\x1b[?25h")  # show cursor for typing
    sys.stdout.write(f"\x1b[{rows};1H\x1b[2K")
    sys.stdout.write(f"Close '{key}' ({_truncate(question, cols - 20)})\n")
    sys.stdout.write("\x1b[2KAnswer (blank cancels): ")
    sys.stdout.flush()
    try:
        answer = input()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    sys.stdout.write("\x1b[?25l")
    sys.stdout.flush()
    if not answer.strip():
        return False, "cancelled"
    return dec.cmd_close(log_path, key, answer=answer.strip())


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(log_path: str) -> int:
    _enable_windows_ansi()
    if msvcrt is None:
        print("decisions-panel.py requires Windows (msvcrt) — not available on this platform.")
        return 1

    selected = 0
    view = "list"  # "list" | "detail"
    open_items: list = []
    malformed = 0

    def reload():
        nonlocal open_items, malformed, selected
        events, malformed = dec.read_events(log_path)
        states = dec.fold_state(events)
        open_items = sort_open_items(states)
        selected = clamp_selection(selected, len(open_items))
        return states

    states = reload()
    last_stat = stat_key(log_path)

    _enter_screen()
    try:
        _draw(render_list(open_items, selected, malformed, log_path))
        while True:
            redraw = False

            cur_stat = stat_key(log_path)
            if has_changed(last_stat, cur_stat):
                last_stat = cur_stat
                states = reload()
                redraw = True

            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ARROW_PREFIXES:
                    ch2 = msvcrt.getwch()
                    if view == "list":
                        if ch2 == ARROW_UP:
                            selected = move_selection(selected, -1, len(open_items))
                            redraw = True
                        elif ch2 == ARROW_DOWN:
                            selected = move_selection(selected, 1, len(open_items))
                            redraw = True
                        elif ch2 == ARROW_LEFT:
                            pass  # already at top level
                    elif view == "detail" and ch2 == ARROW_LEFT:
                        view = "list"
                        redraw = True
                elif ch == "\r":
                    if view == "list" and open_items:
                        view = "detail"
                        redraw = True
                elif ch == ESC:
                    if view == "detail":
                        view = "list"
                        redraw = True
                elif ch in ("q", "Q"):
                    break
                elif ch in ("r", "R"):
                    last_stat = stat_key(log_path)
                    states = reload()
                    redraw = True
                elif ch in ("c", "C"):
                    target = None
                    if view == "list" and open_items:
                        target = open_items[selected]
                    elif view == "detail" and open_items:
                        target = open_items[selected] if selected < len(open_items) else None
                    if target is not None:
                        prompt_close(log_path, target.key, target.question or "")
                        last_stat = stat_key(log_path)
                        states = reload()
                        view = "list"
                        redraw = True

            if redraw:
                if view == "list":
                    _draw(render_list(open_items, selected, malformed, log_path))
                else:
                    current = open_items[selected] if selected < len(open_items) else None
                    if current is None:
                        view = "list"
                        _draw(render_list(open_items, selected, malformed, log_path))
                    else:
                        _draw(render_detail(current, log_path))

            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        _leave_screen()
    return 0


def main(argv=None) -> int:
    log_path = os.environ.get("DECISIONS_LOG_PATH", dec.DEFAULT_LOG_PATH)
    return run(log_path)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        sys.exit(main())
    except Exception:
        # Never let an unhandled exception strand the terminal in the
        # alternate screen buffer with the cursor hidden.
        _leave_screen()
        raise
