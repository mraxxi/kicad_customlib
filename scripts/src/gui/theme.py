"""
Appearance: ttk theme selection, and borrowing the desktop's colours.

Two problems this solves.

First, no ttk theme was ever selected, so Linux got Tk's ``default`` — the
dated Motif-ish one. Choosing ``clam`` on Linux and ``vista`` on Windows is a
visible improvement on its own.

Second, ttk knows nothing about the desktop's colour scheme, and the
``ttk.Combobox`` popup is not even a ttk widget — it is a plain ``tk.Listbox``
outside the theme entirely, styled only through the option database. On KDE,
Plasma writes its scheme to ``kdeglobals`` in a plain INI format, so the real
window, view, selection and alternate-row colours can be read with stdlib
``configparser`` and handed to ttk. That gets correct light/dark and the user's
accent colour with no dependency.

Everything degrades: a missing file, missing keys or an unparseable value just
leaves the chosen ttk theme's own colours in place.
"""

from __future__ import annotations

import configparser
import os
import sys
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any, Dict, Optional, Tuple

# Preferred ttk theme per platform, most-wanted first.
_THEME_PREFERENCE = {
    "linux": ("clam", "alt", "default"),
    "win32": ("vista", "winnative", "xpnative", "clam", "default"),
    "darwin": ("aqua", "clam", "default"),
}

_palette: Optional["Palette"] = None

# Distinguishes "work the palette out yourself" from "there is no palette",
# which a plain None default cannot express.
_UNSET = object()


@dataclass(frozen=True)
class Palette:
    """The handful of colours the interface actually needs."""
    window_bg: str
    window_fg: str
    view_bg: str
    view_fg: str
    view_alt: str
    select_bg: str
    select_fg: str
    negative_fg: str

    @property
    def is_dark(self) -> bool:
        """Whether the scheme is dark, from the window background's luminance."""
        return _luminance(self.window_bg) < 128


def _luminance(hex_colour: str) -> float:
    try:
        r = int(hex_colour[1:3], 16)
        g = int(hex_colour[3:5], 16)
        b = int(hex_colour[5:7], 16)
    except (ValueError, IndexError):
        return 255.0
    # Rec. 601 luma; good enough to tell a dark scheme from a light one.
    return 0.299 * r + 0.587 * g + 0.114 * b


def _rgb_to_hex(value: str) -> Optional[str]:
    """Convert KDE's ``R,G,B`` to ``#rrggbb``."""
    parts = [p.strip() for p in str(value).split(",")]
    if len(parts) < 3:
        return None
    try:
        r, g, b = (max(0, min(255, int(float(p)))) for p in parts[:3])
    except (TypeError, ValueError):
        return None
    return f"#{r:02x}{g:02x}{b:02x}"


def kdeglobals_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "kdeglobals"


def read_kde_palette(path: Optional[Path] = None) -> Optional[Palette]:
    """
    Read Plasma's colour scheme, or None if it cannot be used.

    ``strict=False`` because kdeglobals contains repeated keys and sections
    like ``[Colors:Header][Inactive]`` that the strict parser rejects.
    """
    target = path or kdeglobals_path()
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            parser.read_file(fh)
    except (OSError, configparser.Error):
        return None

    def colour(section: str, key: str) -> Optional[str]:
        try:
            return _rgb_to_hex(parser.get(section, key))
        except (configparser.Error, KeyError):
            return None

    window_bg = colour("Colors:Window", "BackgroundNormal")
    view_bg = colour("Colors:View", "BackgroundNormal")
    if not window_bg or not view_bg:
        # Without at least these two there is nothing worth applying.
        return None

    return Palette(
        window_bg=window_bg,
        window_fg=colour("Colors:Window", "ForegroundNormal") or "#232629",
        view_bg=view_bg,
        view_fg=colour("Colors:View", "ForegroundNormal") or "#232629",
        view_alt=colour("Colors:View", "BackgroundAlternate") or view_bg,
        select_bg=colour("Colors:Selection", "BackgroundNormal") or "#3daee9",
        select_fg=colour("Colors:Selection", "ForegroundNormal") or "#ffffff",
        negative_fg=colour("Colors:View", "ForegroundNegative") or "#da4453",
    )


def current_palette() -> Optional[Palette]:
    """The palette applied by apply_theme(), if any."""
    return _palette


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------

def _choose_theme(style: ttk.Style) -> str:
    available = set(style.theme_names())
    for name in _THEME_PREFERENCE.get(sys.platform, ("clam", "default")):
        if name in available:
            return name
    return style.theme_use()


def row_height() -> int:
    """
    A comfortable Treeview row height for the current font.

    Tk's default is cramped: it leaves no padding around the text at all.
    """
    try:
        return max(20, tkfont.nametofont("TkDefaultFont").metrics("linespace") + 8)
    except tk.TclError:
        return 24


def apply_theme(root: tk.Misc, *, palette: Any = _UNSET) -> Tuple[str, Optional[Palette]]:
    """
    Select a ttk theme and, where possible, align it with the desktop.

    Returns the theme name used and the palette applied, which is None when
    the desktop's scheme could not be read. Omitting `palette` reads the
    scheme; passing None explicitly means "apply no palette", which is why
    the default is a sentinel rather than None. Every step is individually
    guarded, so this is safe on a bare system.
    """
    global _palette

    style = ttk.Style(root)
    theme = _choose_theme(style)
    try:
        style.theme_use(theme)
    except tk.TclError:
        theme = style.theme_use()

    resolved = read_kde_palette() if palette is _UNSET else palette
    _palette = resolved

    # Treeview rows, independent of the colour scheme.
    try:
        style.configure("Treeview", rowheight=row_height())
    except tk.TclError:
        pass

    if resolved is None:
        return theme, None

    try:
        # The root window itself is a tk widget, not a ttk one, so the style
        # below never reaches it and it keeps Tk's default #d9d9d9. That is
        # the colour a resize briefly exposes before the children are laid
        # out again, so leaving it unset flashes grey against the scheme.
        root.winfo_toplevel().configure(background=resolved.window_bg)
    except tk.TclError:
        pass

    try:
        style.configure(".", background=resolved.window_bg,
                        foreground=resolved.window_fg)
        for widget in ("TFrame", "TLabelframe", "TPanedwindow"):
            style.configure(widget, background=resolved.window_bg)
        style.configure("TLabel", background=resolved.window_bg,
                        foreground=resolved.window_fg)
        style.configure("TLabelframe.Label", background=resolved.window_bg,
                        foreground=resolved.window_fg)
        style.configure("TCheckbutton", background=resolved.window_bg,
                        foreground=resolved.window_fg)
        style.configure("TRadiobutton", background=resolved.window_bg,
                        foreground=resolved.window_fg)

        style.configure("Treeview",
                        background=resolved.view_bg,
                        fieldbackground=resolved.view_bg,
                        foreground=resolved.view_fg)
        style.map("Treeview",
                  background=[("selected", resolved.select_bg)],
                  foreground=[("selected", resolved.select_fg)])

        style.configure("TEntry", fieldbackground=resolved.view_bg,
                        foreground=resolved.view_fg)
        style.configure("TCombobox", fieldbackground=resolved.view_bg,
                        foreground=resolved.view_fg)
    except tk.TclError:
        pass

    _style_combobox_popup(root, resolved)
    return theme, resolved


def _style_combobox_popup(root: tk.Misc, palette: Palette) -> None:
    """
    Style the combobox dropdown.

    The popup is a tk.Listbox, not a ttk widget, so ttk.Style cannot reach it
    and it otherwise renders with Tk's stark defaults next to a themed entry.
    The option database is the only way in.
    """
    try:
        root.option_add("*TCombobox*Listbox.background", palette.view_bg)
        root.option_add("*TCombobox*Listbox.foreground", palette.view_fg)
        root.option_add("*TCombobox*Listbox.selectBackground", palette.select_bg)
        root.option_add("*TCombobox*Listbox.selectForeground", palette.select_fg)
        root.option_add("*TCombobox*Listbox.font", "TkDefaultFont")
        root.option_add("*TCombobox*Listbox.borderWidth", "1")
        # The category list in the browser is a bare Listbox too.
        root.option_add("*Listbox.background", palette.view_bg)
        root.option_add("*Listbox.foreground", palette.view_fg)
        root.option_add("*Listbox.selectBackground", palette.select_bg)
        root.option_add("*Listbox.selectForeground", palette.select_fg)
    except tk.TclError:
        pass


def striping_colours() -> Optional[Dict[str, str]]:
    """Background colours for alternating Treeview rows, if a palette is set."""
    if _palette is None:
        return None
    return {"even": _palette.view_bg, "odd": _palette.view_alt}


def problem_foreground() -> Optional[str]:
    """Foreground for a row with a dangling reference, if a palette is set."""
    return _palette.negative_fg if _palette else None


def enable_windows_hidpi() -> bool:
    """
    Ask Windows for per-monitor DPI awareness.

    Without this Tk renders blurry on a HiDPI display. It must be called
    before the Tk root exists, so this lives here rather than in apply_theme.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return True
    except Exception:  # noqa: BLE001 -- older Windows, or no shcore
        return False
