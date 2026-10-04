"""
File dialogs, native where one is available.

On Linux, ``tkinter.filedialog`` does not use a native dialog — Tk draws its
own Motif-era widget, and no amount of ttk theming changes it. On KDE,
``kdialog`` (part of Plasma) gives the real Plasma file dialog; ``zenity``
gives the GTK one. Both are external *binaries*, not Python dependencies, so
the stdlib-only rule holds and everything still works with neither installed.

On Windows and macOS, Tk's dialog *is* the native one, so it is used directly.

Set ``KICAD_CUSTOMLIB_FILEPICKER`` to ``tk``, ``kdialog``, ``zenity`` or
``auto`` to override the choice.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tkinter import filedialog
from typing import List, Optional, Sequence, Tuple

ENV_OVERRIDE = "KICAD_CUSTOMLIB_FILEPICKER"

BACKEND_TK = "tk"
BACKEND_KDIALOG = "kdialog"
BACKEND_ZENITY = "zenity"

# A file dialog legitimately stays open for minutes while someone browses, so
# this is a backstop against a wedged helper, not a response-time limit.
HELPER_TIMEOUT_S = 600

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

    for candidate in (BACKEND_KDIALOG, BACKEND_ZENITY):
        if candidate not in _failed_backends and shutil.which(candidate):
            return candidate
    return BACKEND_TK


# --------------------------------------------------------------------------
# Running a helper
# --------------------------------------------------------------------------

def _run_helper(argv: Sequence[str], parent=None) -> Optional[str]:
    """
    Run a dialog helper, keeping the Tk window repainting while it is open.

    Returns stdout on success, None on cancel, and raises OSError if the
    helper could not be run at all so the caller can fall back.

    The event-pumping loop matters: a plain blocking wait leaves the main
    window unrepainted for as long as the dialog is open, which on some
    compositors shows as a grey rectangle.
    """
    proc = subprocess.Popen(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + HELPER_TIMEOUT_S
    while proc.poll() is None:
        if time.monotonic() > deadline:
            proc.kill()
            raise OSError(f"{argv[0]} did not return within {HELPER_TIMEOUT_S}s")
        if parent is not None:
            try:
                parent.update()
            except Exception:  # noqa: BLE001 -- window closed under us
                parent = None
        time.sleep(0.05)

    stdout, _stderr = proc.communicate()
    if proc.returncode == 0:
        return stdout
    # Both helpers use a non-zero exit for "the user cancelled", which is a
    # normal outcome and not a failure.
    if proc.returncode == 1:
        return None
    raise OSError(f"{argv[0]} exited with {proc.returncode}")


def _kdialog_filters(filters: Sequence[Filter]) -> str:
    # kdialog wants "patterns|label", several joined by newlines.
    return "\n".join(f"{patterns}|{label}" for label, patterns in filters)


def _zenity_filter_args(filters: Sequence[Filter]) -> List[str]:
    return [f"--file-filter={label} | {patterns}" for label, patterns in filters]


def _parse_paths(stdout: Optional[str]) -> List[Path]:
    if not stdout:
        return []
    return [Path(line.strip()) for line in stdout.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def open_files(
    parent=None,
    *,
    title: str = "Select files",
    initialdir: Optional[Path] = None,
    filters: Sequence[Filter] = KICAD_FILTERS,
    multiple: bool = True,
) -> List[Path]:
    """Ask for one or more existing files. Empty list means cancelled."""
    start = str(initialdir) if initialdir else str(Path.home())
    backend = available_backend()

    if backend == BACKEND_KDIALOG:
        argv = ["kdialog", "--title", title]
        if multiple:
            argv += ["--multiple", "--separate-output"]
        argv += ["--getopenfilename", start, _kdialog_filters(filters)]
        try:
            return _parse_paths(_run_helper(argv, parent))
        except OSError:
            _failed_backends.add(BACKEND_KDIALOG)

    elif backend == BACKEND_ZENITY:
        argv = ["zenity", "--file-selection", f"--title={title}",
                f"--filename={start}{os.sep}"]
        if multiple:
            argv += ["--multiple", "--separator=\n"]
        argv += _zenity_filter_args(filters)
        try:
            return _parse_paths(_run_helper(argv, parent))
        except OSError:
            _failed_backends.add(BACKEND_ZENITY)

    return _tk_open_files(parent, title, start, filters, multiple)


def open_directory(
    parent=None,
    *,
    title: str = "Select a folder",
    initialdir: Optional[Path] = None,
) -> Optional[Path]:
    """Ask for an existing directory. None means cancelled."""
    start = str(initialdir) if initialdir else str(Path.home())
    backend = available_backend()

    if backend == BACKEND_KDIALOG:
        argv = ["kdialog", "--title", title, "--getexistingdirectory", start]
        try:
            paths = _parse_paths(_run_helper(argv, parent))
            return paths[0] if paths else None
        except OSError:
            _failed_backends.add(BACKEND_KDIALOG)

    elif backend == BACKEND_ZENITY:
        argv = ["zenity", "--file-selection", "--directory",
                f"--title={title}", f"--filename={start}{os.sep}"]
        try:
            paths = _parse_paths(_run_helper(argv, parent))
            return paths[0] if paths else None
        except OSError:
            _failed_backends.add(BACKEND_ZENITY)

    chosen = filedialog.askdirectory(parent=parent, title=title, initialdir=start)
    return Path(chosen) if chosen else None


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
