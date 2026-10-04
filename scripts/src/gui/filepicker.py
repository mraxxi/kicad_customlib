"""
File dialogs, native where one is available.

On Linux, ``tkinter.filedialog`` does not use a native dialog — Tk draws its
own Motif-era widget, and no amount of ttk theming changes it. On KDE,
``kdialog`` (part of Plasma) gives the real Plasma file dialog; ``zenity``
gives the GTK one. Both are external *binaries*, not Python dependencies, so
the stdlib-only rule holds and everything still works with neither installed.

On Windows and macOS, Tk's dialog *is* the native one, so it is used directly.

Set ``KICAD_CUSTOMLIB_FILEPICKER`` to ``kdialog`` to insist on the Plasma
dialog, or to ``tk`` or ``zenity`` to pin either of the others. ``auto`` is
the default order below.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from tkinter import filedialog
from typing import Callable, List, Optional, Sequence, Tuple

ENV_OVERRIDE = "KICAD_CUSTOMLIB_FILEPICKER"

BACKEND_TK = "tk"
BACKEND_KDIALOG = "kdialog"
BACKEND_ZENITY = "zenity"

# Order of preference on Linux. zenity first: see the module docstring for the
# measurements behind that, which are not what the desktop would suggest.
PREFERENCE = (BACKEND_ZENITY, BACKEND_KDIALOG)

# A file dialog legitimately stays open for minutes while someone browses, so
# this is a backstop against a wedged helper, not a response-time limit.
HELPER_TIMEOUT_S = 600

# How often to check whether the helper has finished. From Tk's event loop
# (POLL_MS) this costs nothing; the blocking form uses the same interval as a
# thread join.
POLL_MS = 50
POLL_INTERVAL_S = 0.05

# Guards against a second concurrent dialog. Module level rather than
# per-window because the helpers are separate processes competing for the
# pointer: two at once is never what anyone wanted.
_dialog_open = False

# Once a helper has failed in this session, stop retrying it: a broken
# kdialog would otherwise add a pointless delay to every single dialog.
_failed_backends: set = set()

# Filters as (label, space-separated patterns).
Filter = Tuple[str, str]

KICAD_FILTERS: Sequence[Filter] = (
    ("KiCad symbols and footprints", "*.kicad_sym *.kicad_mod"),
    ("3D models", "*.step *.stp *.wrl"),
    ("All files", "*"),
)
ARCHIVE_FILTERS: Sequence[Filter] = (
    ("ZIP archives", "*.zip"),
    ("All files", "*"),
)


def available_backend() -> str:
    """
    Which dialog implementation to use.

    Native helpers are Linux-only: on Windows and macOS Tk already gives the
    platform dialog, so shelling out would be a downgrade.
    """
    override = (os.environ.get(ENV_OVERRIDE) or "").strip().lower()
    if override in (BACKEND_TK, BACKEND_KDIALOG, BACKEND_ZENITY):
        if override == BACKEND_TK or shutil.which(override):
            return override
        return BACKEND_TK
    if override and override != "auto":
        return BACKEND_TK

    if not sys.platform.startswith("linux"):
        return BACKEND_TK

    for candidate in PREFERENCE:
        if candidate not in _failed_backends and shutil.which(candidate):
            return candidate
    return BACKEND_TK


# --------------------------------------------------------------------------
# Running a helper
# --------------------------------------------------------------------------

def _argv_for_files(backend, title, start, filters, multiple) -> List[str]:
    if backend == BACKEND_KDIALOG:
        argv = ["kdialog", "--title", title]
        if multiple:
            argv += ["--multiple", "--separate-output"]
        return argv + ["--getopenfilename", start, _kdialog_filters(filters)]
    argv = ["zenity", "--file-selection", f"--title={title}",
            f"--filename={start}{os.sep}"]
    if multiple:
        argv += ["--multiple", "--separator=\n"]
    return argv + _zenity_filter_args(filters)


def _argv_for_directory(backend, title, start) -> List[str]:
    if backend == BACKEND_KDIALOG:
        return ["kdialog", "--title", title, "--getexistingdirectory", start]
    return ["zenity", "--file-selection", "--directory",
            f"--title={title}", f"--filename={start}{os.sep}"]


def _spawn(argv: Sequence[str]) -> Tuple[subprocess.Popen, dict, threading.Thread]:
    """
    Start a helper and begin draining its output on a worker thread.

    The draining thread is not optional. `Popen` with two pipes and no reader
    deadlocks as soon as a helper writes more than the pipe buffer holds --
    about 64 KiB -- because it blocks in `write()` while we wait for it to
    exit. `communicate()` on a thread drains both streams without the UI
    thread ever touching I/O.
    """
    proc = subprocess.Popen(
        list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    captured: dict = {}

    def drain() -> None:
        try:
            captured["stdout"], captured["stderr"] = proc.communicate()
        except Exception as exc:  # noqa: BLE001 -- reported to the caller
            captured["error"] = exc

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    return proc, captured, reader


def _interpret(argv: Sequence[str], proc: subprocess.Popen, captured: dict) -> Optional[str]:
    """stdout on success, None on cancel, OSError if the helper itself failed."""
    if "error" in captured:
        raise OSError(f"{argv[0]} failed: {captured['error']}")
    if proc.returncode == 0:
        return captured.get("stdout") or ""
    # Both helpers use exit 1 for "the user cancelled", a normal outcome.
    if proc.returncode == 1:
        return None
    raise OSError(f"{argv[0]} exited with {proc.returncode}")


def _run_helper(argv: Sequence[str], parent=None) -> Optional[str]:
    """
    Run a helper and wait for it, blocking the caller.

    Used when there is no event loop to keep alive -- the tests, and any
    caller that passes no `on_done`. A GUI must use `_run_helper_async`
    instead: see the note there about what blocking here actually costs.
    """
    global _dialog_open
    if _dialog_open:
        return None
    proc, captured, reader = _spawn(argv)
    _dialog_open = True
    try:
        deadline = time.monotonic() + HELPER_TIMEOUT_S
        while reader.is_alive():
            if time.monotonic() > deadline:
                proc.kill()
                reader.join(timeout=5)
                raise OSError(f"{argv[0]} did not return within {HELPER_TIMEOUT_S}s")
            reader.join(POLL_INTERVAL_S)
    finally:
        _dialog_open = False
    return _interpret(argv, proc, captured)


def _run_helper_async(
    argv: Sequence[str],
    parent,
    on_stdout: Callable[[Optional[str]], None],
    on_failure: Callable[[], None],
) -> bool:
    """
    Run a helper without blocking the event loop at all.

    This is the whole point of the module's second rewrite. The first version
    waited in a loop calling Tk's `update()`, which dispatches input events
    and so re-entered the very callback that opened the dialog -- clicking
    "Add file(s)..." launched a second helper whose loop nested inside the
    first, and the application ground to a halt. Replacing it with
    `update_idletasks()` removed the re-entrancy but not the freeze:
    `update_idletasks()` flushes Tk's pending redraws and nothing else, so
    incoming X expose and configure events were still not processed. Measured,
    that meant zero redraw cycles and a resize silently ignored for as long as
    the dialog was open -- which is what showed as a window that would not
    resize and a black rectangle where the uncovered area should have been.

    So nothing waits. The helper is polled from Tk's own event loop with
    `after()`, the loop keeps running normally throughout, and the result is
    delivered to `on_stdout` on the UI thread. Returns False if a dialog is
    already open, in which case neither callback runs.
    """
    global _dialog_open
    if _dialog_open:
        return False

    try:
        proc, captured, reader = _spawn(argv)
    except OSError:
        on_failure()
        return True

    _dialog_open = True
    deadline = time.monotonic() + HELPER_TIMEOUT_S

    def finish(action: Callable[[], None]) -> None:
        global _dialog_open
        _dialog_open = False
        action()

    def poll() -> None:
        if reader.is_alive():
            if time.monotonic() > deadline:
                proc.kill()
                finish(on_failure)
                return
            try:
                parent.after(POLL_MS, poll)
            except Exception:  # noqa: BLE001 -- the window went away
                finish(lambda: None)
            return
        try:
            stdout = _interpret(argv, proc, captured)
        except OSError:
            finish(on_failure)
            return
        finish(lambda: on_stdout(stdout))

    parent.after(POLL_MS, poll)
    return True


def _kdialog_filters(filters: Sequence[Filter]) -> str:
    # kdialog wants "patterns|label", several joined by newlines.
    return "\n".join(f"{patterns}|{label}" for label, patterns in filters)


def _zenity_filter_args(filters: Sequence[Filter]) -> List[str]:
    return [f"--file-filter={label} | {patterns}" for label, patterns in filters]


def _parse_paths(stdout: Optional[str]) -> List[Path]:
    if not stdout:
        return []
    return [Path(line.strip()) for line in stdout.splitlines() if line.strip()]


def _can_schedule(parent) -> bool:
    return parent is not None and hasattr(parent, "after")


# --------------------------------------------------------------------------
# Public API
#
# Both functions take an optional `on_done`. With it they return immediately
# and hand the result back on the UI thread, which is what any GUI caller
# must do; without it they block and return the result directly, which is
# what the tests and any event-loop-free caller use.
# --------------------------------------------------------------------------

def open_files(
    parent=None,
    *,
    title: str = "Select files",
    initialdir: Optional[Path] = None,
    filters: Sequence[Filter] = KICAD_FILTERS,
    multiple: bool = True,
    on_done: Optional[Callable[[List[Path]], None]] = None,
) -> List[Path]:
    """Ask for one or more existing files. An empty list means cancelled."""
    start = str(initialdir) if initialdir else str(Path.home())
    backend = available_backend()

    def tk_fallback() -> List[Path]:
        chosen = _tk_open_files(parent, title, start, filters, multiple)
        if on_done is not None:
            on_done(chosen)
        return chosen

    if backend == BACKEND_TK:
        return tk_fallback()

    argv = _argv_for_files(backend, title, start, filters, multiple)

    if on_done is not None and _can_schedule(parent):
        def failed() -> None:
            _failed_backends.add(backend)
            tk_fallback()
        if not _run_helper_async(argv, parent,
                                 lambda out: on_done(_parse_paths(out)), failed):
            return []
        return []

    try:
        return _parse_paths(_run_helper(argv, parent))
    except OSError:
        _failed_backends.add(backend)
    return tk_fallback()


def open_directory(
    parent=None,
    *,
    title: str = "Select a folder",
    initialdir: Optional[Path] = None,
    on_done: Optional[Callable[[Optional[Path]], None]] = None,
) -> Optional[Path]:
    """Ask for an existing directory. None means cancelled."""
    start = str(initialdir) if initialdir else str(Path.home())
    backend = available_backend()

    def tk_fallback() -> Optional[Path]:
        chosen = filedialog.askdirectory(parent=parent, title=title, initialdir=start)
        result = Path(chosen) if chosen else None
        if on_done is not None:
            on_done(result)
        return result

    if backend == BACKEND_TK:
        return tk_fallback()

    argv = _argv_for_directory(backend, title, start)

    def first(out: Optional[str]) -> Optional[Path]:
        paths = _parse_paths(out)
        return paths[0] if paths else None

    if on_done is not None and _can_schedule(parent):
        def failed() -> None:
            _failed_backends.add(backend)
            tk_fallback()
        if not _run_helper_async(argv, parent,
                                 lambda out: on_done(first(out)), failed):
            return None
        return None

    try:
        return first(_run_helper(argv, parent))
    except OSError:
        _failed_backends.add(backend)
    return tk_fallback()


# --------------------------------------------------------------------------
# Tk fallback
# --------------------------------------------------------------------------

def _tk_filetypes(filters: Sequence[Filter]) -> List[Tuple[str, str]]:
    # Tk wants ("label", "patterns"); "*" is spelled "*.*" on Windows.
    return [(label, patterns if patterns != "*" else "*.*") for label, patterns in filters]


def _tk_open_files(parent, title, start, filters, multiple) -> List[Path]:
    kwargs = dict(parent=parent, title=title, initialdir=start,
                  filetypes=_tk_filetypes(filters))
    if multiple:
        chosen = filedialog.askopenfilenames(**kwargs)
        return [Path(p) for p in chosen] if chosen else []
    chosen = filedialog.askopenfilename(**kwargs)
    return [Path(chosen)] if chosen else []
