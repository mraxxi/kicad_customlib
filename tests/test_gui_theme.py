"""Phase 2: reading the desktop colour scheme. No display needed."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.gui import theme

# A trimmed copy of the real Breeze scheme as Plasma writes it, including the
# quirks that defeat a strict INI parser: a repeated key and a section name
# with a trailing bracketed qualifier.
BREEZE = """\
[ColorEffects:Disabled]
Color=56,56,56
ColorAmount=0
ColorAmount=0

[Colors:Button]
BackgroundNormal=252,252,252
ForegroundNormal=35,38,41

[Colors:Header][Inactive]
BackgroundNormal=239,240,241

[Colors:Selection]
BackgroundNormal=61,174,233
ForegroundNormal=255,255,255

[Colors:View]
BackgroundAlternate=247,247,247
BackgroundNormal=255,255,255
ForegroundNegative=218,68,83
ForegroundNormal=35,38,41

[Colors:Window]
BackgroundAlternate=227,229,231
BackgroundNormal=239,240,241
ForegroundNormal=35,38,41
"""

BREEZE_DARK = """\
[Colors:Selection]
BackgroundNormal=61,174,233
ForegroundNormal=255,255,255

[Colors:View]
BackgroundAlternate=42,46,50
BackgroundNormal=27,30,32
ForegroundNormal=252,252,252

[Colors:Window]
BackgroundNormal=42,46,50
ForegroundNormal=252,252,252
"""


@pytest.fixture
def scheme(tmp_path):
    def write(content: str) -> Path:
        path = tmp_path / "kdeglobals"
        path.write_text(content)
        return path
    return write


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def test_reads_the_breeze_scheme(scheme):
    p = theme.read_kde_palette(scheme(BREEZE))
    assert p is not None
    assert p.window_bg == "#eff0f1"
    assert p.window_fg == "#232629"
    assert p.view_bg == "#ffffff"
    assert p.view_alt == "#f7f7f7"
    assert p.select_bg == "#3daee9"
    assert p.select_fg == "#ffffff"
    assert p.negative_fg == "#da4453"
    assert p.is_dark is False


def test_a_repeated_key_and_bracketed_section_do_not_break_parsing(scheme):
    """
    kdeglobals really does contain both. A strict parser raises on them, which
    would mean silently losing the whole scheme.
    """
    assert theme.read_kde_palette(scheme(BREEZE)) is not None


def test_a_dark_scheme_is_detected(scheme):
    p = theme.read_kde_palette(scheme(BREEZE_DARK))
    assert p.is_dark is True
    assert p.view_bg == "#1b1e20"


def test_a_missing_file_yields_no_palette(tmp_path):
    assert theme.read_kde_palette(tmp_path / "absent") is None


def test_a_file_without_colour_sections_yields_no_palette(scheme):
    assert theme.read_kde_palette(scheme("[General]\nwidgetStyle=Breeze\n")) is None


def test_garbage_yields_no_palette_rather_than_raising(scheme):
    assert theme.read_kde_palette(scheme("not an ini file at all\x00\x01")) is None


def test_missing_optional_keys_fall_back_to_defaults(scheme):
    minimal = (
        "[Colors:Window]\nBackgroundNormal=239,240,241\n\n"
        "[Colors:View]\nBackgroundNormal=255,255,255\n"
    )
    p = theme.read_kde_palette(scheme(minimal))
    assert p.view_alt == "#ffffff"          # falls back to the view background
    assert p.select_bg == "#3daee9"         # Breeze accent as the default
    assert p.negative_fg == "#da4453"


def test_an_unparseable_colour_value_is_ignored(scheme):
    broken = (
        "[Colors:Window]\nBackgroundNormal=not,a,colour\n\n"
        "[Colors:View]\nBackgroundNormal=255,255,255\n"
    )
    # window_bg cannot be read, so there is nothing worth applying.
    assert theme.read_kde_palette(scheme(broken)) is None


def test_kdeglobals_path_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert theme.kdeglobals_path() == tmp_path / "kdeglobals"


@pytest.mark.parametrize("value,expected", [
    ("0,0,0", "#000000"),
    ("255,255,255", "#ffffff"),
    ("61,174,233", "#3daee9"),
    ("300,-5,10", "#ff000a"),          # clamped
    ("61, 174, 233", "#3daee9"),       # spaces tolerated
    ("61,174,233,255", "#3daee9"),     # alpha ignored
])
def test_rgb_conversion(value, expected):
    assert theme._rgb_to_hex(value) == expected


@pytest.mark.parametrize("value", ["", "61,174", "x", "a,b,c"])
def test_rgb_conversion_rejects_junk(value):
    assert theme._rgb_to_hex(value) is None


# --------------------------------------------------------------------------
# Applying (needs Tk; skipped without a display)
# --------------------------------------------------------------------------

def _display_available() -> bool:
    try:
        import tkinter
        root = tkinter.Tk()
    except Exception:  # noqa: BLE001
        return False
    root.destroy()
    return True


needs_display = pytest.mark.skipif(
    not _display_available(), reason="no display available for Tk"
)


@needs_display
def test_apply_theme_selects_a_real_theme(scheme):
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.withdraw()
    try:
        name, palette = theme.apply_theme(
            root, palette=theme.read_kde_palette(scheme(BREEZE))
        )
        assert name in ttk.Style(root).theme_names()
        if sys.platform.startswith("linux"):
            # clam, rather than the dated Motif-ish "default" Tk would pick.
            assert name == "clam"
        assert palette is not None
    finally:
        root.destroy()


@needs_display
def test_apply_theme_feeds_the_palette_to_the_treeview(scheme):
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.withdraw()
    try:
        theme.apply_theme(root, palette=theme.read_kde_palette(scheme(BREEZE)))
        style = ttk.Style(root)
        assert style.lookup("Treeview", "background") == "#ffffff"
        assert ("selected", "#3daee9") in style.map("Treeview", "background")
        assert theme.striping_colours() == {"even": "#ffffff", "odd": "#f7f7f7"}
        assert theme.problem_foreground() == "#da4453"
    finally:
        root.destroy()


@needs_display
def test_apply_theme_works_with_no_palette_at_all(tmp_path):
    """A machine that is not KDE still gets the better ttk theme."""
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    try:
        theme._palette = None
        name, palette = theme.apply_theme(
            root, palette=theme.read_kde_palette(tmp_path / "absent")
        )
        assert name
        assert palette is None
        assert theme.striping_colours() is None
        assert theme.problem_foreground() is None
    finally:
        root.destroy()


@needs_display
def test_row_height_leaves_room_around_the_text():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    try:
        from tkinter import font as tkfont
        assert theme.row_height() > tkfont.nametofont("TkDefaultFont").metrics("linespace")
    finally:
        root.destroy()


def test_windows_hidpi_hook_is_a_noop_elsewhere(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert theme.enable_windows_hidpi() is False


@needs_display
def test_the_root_window_background_matches_the_scheme(scheme):
    """
    The root is a tk widget, not a ttk one, so ttk.Style never reaches it.
    Left unset it keeps Tk's #d9d9d9, which is the colour a resize exposes
    for an instant before the children are laid out again.
    """
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    try:
        theme.apply_theme(root, palette=theme.read_kde_palette(scheme(BREEZE)))
        assert root.cget("background") == "#eff0f1"
    finally:
        root.destroy()


@needs_display
def test_a_dark_scheme_darkens_the_root_too(scheme):
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    try:
        theme.apply_theme(root, palette=theme.read_kde_palette(scheme(BREEZE_DARK)))
        assert root.cget("background") == "#2a2e32"
    finally:
        root.destroy()
