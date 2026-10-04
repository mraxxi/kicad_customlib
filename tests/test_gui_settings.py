"""Phase 1: GUI preferences and geometry, tested without a display."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.gui import settings as st


@pytest.fixture
def cfg(tmp_path) -> Path:
    return tmp_path / "gui.json"


class FakeScreen:
    """Stands in for a Tk widget, so profile keying needs no display."""

    def __init__(self, width=3840, height=1080, dpi=96.0):
        self._w, self._h, self._dpi = width, height, dpi

    def winfo_screenwidth(self):
        return self._w

    def winfo_screenheight(self):
        return self._h

    def winfo_fpixels(self, _spec):
        return self._dpi


# --------------------------------------------------------------------------
# Location
# --------------------------------------------------------------------------

def test_config_lives_outside_the_repository(monkeypatch, tmp_path):
    """
    None of this should be committed: a window size from a 4K desktop is
    actively wrong on a laptop.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert st.config_path() == tmp_path / "xdg" / "kicad_customlib" / "gui.json"


def test_config_falls_back_to_dot_config(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert st.config_path().parts[-3:] == (".config", "kicad_customlib", "gui.json")


# --------------------------------------------------------------------------
# Failing soft — the opposite of provenance.json
# --------------------------------------------------------------------------

@pytest.mark.parametrize("content", [
    "", "   ", "{broken", "[]", "null", '{"version": 999}', '"a string"',
])
def test_a_damaged_config_yields_defaults_without_raising(cfg, content):
    """
    Preferences are disposable and reconstructible by using the app, so a bad
    file must never stop the GUI from opening. provenance.json raises loudly
    for the opposite reason; that asymmetry is deliberate.
    """
    cfg.write_text(content)
    s = st.Settings.load(cfg)
    assert s.data["version"] == st.SCHEMA_VERSION
    assert s.data["dirs"] == {} and s.data["geometry"] == {}


def test_the_pre_versioned_config_is_migrated_not_discarded(cfg):
    """
    The first release wrote a bare {"last_category": ...} with no version
    field. Rejecting it on version grounds would silently forget the user's
    remembered category.
    """
    cfg.write_text(json.dumps({"last_category": "Custom_Generic-Heatsinks"}))
    s = st.Settings.load(cfg)
    assert s.get("last_category") == "Custom_Generic-Heatsinks"
    assert s.data["version"] == st.SCHEMA_VERSION
    assert s.data["geometry"] == {}


def test_migration_ignores_unknown_legacy_keys(cfg):
    cfg.write_text(json.dumps({"last_category": "X", "something_else": 42}))
    s = st.Settings.load(cfg)
    assert s.get("last_category") == "X"
    assert "something_else" not in s.data


def test_a_missing_config_yields_defaults(cfg):
    assert st.Settings.load(cfg).data["geometry"] == {}


def test_an_unreadable_config_yields_defaults(cfg):
    cfg.mkdir()                  # a directory where a file should be
    assert st.Settings.load(cfg).data["geometry"] == {}


def test_a_config_with_wrong_section_types_is_normalised(cfg):
    cfg.write_text(json.dumps({"version": 1, "dirs": "nope", "geometry": 5}))
    s = st.Settings.load(cfg)
    assert s.data["dirs"] == {} and s.data["geometry"] == {}


def test_save_failure_is_reported_not_raised(tmp_path):
    s = st.Settings(path=tmp_path / "nonexistent-dir-file" / "x" / "gui.json")
    (tmp_path / "nonexistent-dir-file").write_text("I am a file, not a directory")
    assert s.save() is False


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------

def test_round_trip(cfg):
    s = st.Settings.load(cfg)
    s.set("last_category", "TPAxxxx-TI_Audio_Amps")
    s.set_window_geometry("3840x1080@96", st.WINDOW_MAIN, "1180x720+10+20")
    s.set_sash("3840x1080@96", "browser", 310)
    s.set_columns("3840x1080@96", "symbol", {"Name": 240, "Category": 150})
    assert s.save() is True

    again = st.Settings.load(cfg)
    assert again.get("last_category") == "TPAxxxx-TI_Audio_Amps"
    assert again.window_geometry("3840x1080@96", st.WINDOW_MAIN) == "1180x720+10+20"
    assert again.sash("3840x1080@96", "browser") == 310
    assert again.columns("3840x1080@96", "symbol") == {"Name": 240, "Category": 150}


def test_saving_twice_unchanged_is_byte_identical(cfg):
    s = st.Settings.load(cfg)
    s.set_sash("k", "browser", 300)
    s.save()
    first = cfg.read_bytes()
    st.Settings.load(cfg).save()
    assert cfg.read_bytes() == first


def test_saved_file_uses_lf(cfg):
    s = st.Settings.load(cfg)
    s.set("last_category", "X")
    s.save()
    assert b"\r" not in cfg.read_bytes()


# --------------------------------------------------------------------------
# Screen profiles
# --------------------------------------------------------------------------

def test_profile_key_format():
    assert st.Settings.profile_key(FakeScreen(1920, 1080, 96.0)) == "1920x1080@96"


def test_attaching_a_monitor_changes_the_key():
    """
    Under X11 the screen size is the bounding box of all monitors, so a new
    display yields a new profile and the old one is left untouched.
    """
    laptop = st.Settings.profile_key(FakeScreen(1920, 1080))
    with_external = st.Settings.profile_key(FakeScreen(3840, 1080))
    assert laptop != with_external


def test_a_dpi_change_at_the_same_size_also_changes_the_key():
    assert st.Settings.profile_key(FakeScreen(1920, 1080, 96.0)) != \
        st.Settings.profile_key(FakeScreen(1920, 1080, 192.0))


def test_profile_key_survives_a_widget_with_no_display():
    class Broken:
        def winfo_screenwidth(self):
            raise RuntimeError("no display")
    assert st.Settings.profile_key(Broken()) == "unknown"


def test_profiles_are_independent(cfg):
    s = st.Settings.load(cfg)
    s.set_window_geometry("1920x1080@96", st.WINDOW_MAIN, "1000x600")
    s.set_window_geometry("3840x1080@96", st.WINDOW_MAIN, "1800x900")
    assert s.window_geometry("1920x1080@96", st.WINDOW_MAIN) == "1000x600"
    assert s.window_geometry("3840x1080@96", st.WINDOW_MAIN) == "1800x900"


def test_an_unknown_profile_reads_as_empty_not_an_error(cfg):
    s = st.Settings.load(cfg)
    assert s.window_geometry("never-seen", st.WINDOW_MAIN) is None
    assert s.sash("never-seen", "browser") is None
    assert s.columns("never-seen", "symbol") == {}


def test_reading_a_profile_does_not_create_it(cfg):
    """
    Otherwise merely opening the app on a new screen grows the file, and the
    first read after reset_layout() silently undoes it.
    """
    s = st.Settings.load(cfg)
    s.columns("never-seen", "symbol")
    s.sash("never-seen", "browser")
    s.window_geometry("never-seen", st.WINDOW_MAIN)
    s.browser_state("never-seen")
    assert s.data["geometry"] == {}


def test_reset_layout_is_not_undone_by_a_subsequent_read(cfg):
    s = st.Settings.load(cfg)
    s.set_sash("k", "browser", 300)
    s.reset_layout()
    s.columns("k", "symbol")
    assert s.data["geometry"] == {}


def test_reset_layout_clears_geometry_but_keeps_preferences(cfg):
    s = st.Settings.load(cfg)
    s.set("last_category", "Keep_Me")
    s.remember_dir(st.DIR_IMPORT_SOURCE, Path(cfg.parent))
    s.set_window_geometry("k", st.WINDOW_MAIN, "1x1")
    s.reset_layout()
    assert s.data["geometry"] == {}
    assert s.get("last_category") == "Keep_Me"
    assert s.dir_for(st.DIR_IMPORT_SOURCE, Path("/fallback")) == cfg.parent


# --------------------------------------------------------------------------
# Remembered directories
# --------------------------------------------------------------------------

def test_dir_for_returns_the_default_when_nothing_is_remembered(cfg, tmp_path):
    assert st.Settings.load(cfg).dir_for(st.DIR_IMPORT_SOURCE, tmp_path) == tmp_path


def test_remember_dir_stores_the_containing_directory_of_a_file(cfg, tmp_path):
    f = tmp_path / "part.zip"
    f.write_text("x")
    s = st.Settings.load(cfg)
    s.remember_dir(st.DIR_IMPORT_SOURCE, f)
    assert s.dir_for(st.DIR_IMPORT_SOURCE, Path("/fallback")) == tmp_path


def test_remember_dir_stores_a_directory_as_itself(cfg, tmp_path):
    s = st.Settings.load(cfg)
    s.remember_dir(st.DIR_PACKAGE_PROJECT, tmp_path)
    assert s.dir_for(st.DIR_PACKAGE_PROJECT, Path("/fallback")) == tmp_path


def test_a_remembered_directory_that_has_vanished_falls_back(cfg, tmp_path):
    """Better the default than dropping the user at a dead end."""
    gone = tmp_path / "gone"
    gone.mkdir()
    s = st.Settings.load(cfg)
    s.remember_dir(st.DIR_IMPORT_SOURCE, gone)
    gone.rmdir()
    assert s.dir_for(st.DIR_IMPORT_SOURCE, tmp_path) == tmp_path


def test_purposes_are_independent(cfg, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    s = st.Settings.load(cfg)
    s.remember_dir(st.DIR_IMPORT_SOURCE, a)
    s.remember_dir(st.DIR_PACKAGE_PROJECT, b)
    assert s.dir_for(st.DIR_IMPORT_SOURCE, tmp_path) == a
    assert s.dir_for(st.DIR_PACKAGE_PROJECT, tmp_path) == b


# --------------------------------------------------------------------------
# Columns and browser state
# --------------------------------------------------------------------------

def test_column_widths_are_per_kind(cfg):
    s = st.Settings.load(cfg)
    s.set_columns("k", "symbol", {"Name": 240})
    s.set_columns("k", "footprint", {"Name": 300})
    assert s.columns("k", "symbol") == {"Name": 240}
    assert s.columns("k", "footprint") == {"Name": 300}


def test_nonsense_column_widths_are_ignored(cfg):
    s = st.Settings.load(cfg)
    s.profile("k", create=True)["columns"]["symbol"] = {
        "Name": "wide", "Category": -5, "Good": 120
    }
    assert s.columns("k", "symbol") == {"Good": 120}


def test_browser_state_round_trips_including_all_categories(cfg):
    s = st.Settings.load(cfg)
    s.set_browser_state("k", kind="footprint", category=None,
                        sort_column="Name", sort_reverse=True)
    state = s.browser_state("k")
    assert state["kind"] == "footprint"
    assert state["category"] is None       # "[All categories]" is a real choice
    assert state["sort"] == ["Name", True]


def test_browser_state_does_not_remember_the_search_box(cfg):
    """A filter silently applied at launch looks like a library gone missing."""
    s = st.Settings.load(cfg)
    s.set_browser_state("k", kind="symbol", category="Amp")
    assert "search" not in s.browser_state("k")


# --------------------------------------------------------------------------
# Geometry maths
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("1180x720", {"width": 1180, "height": 720}),
    ("1180x720+120+80", {"width": 1180, "height": 720, "x": 120, "y": 80}),
    ("1180x720-10-20", {"width": 1180, "height": 720, "x": -10, "y": -20}),
])
def test_parse_geometry_accepts_tk_forms(value, expected):
    assert st.parse_geometry(value) == expected


@pytest.mark.parametrize("value", ["", "garbage", "0x600", "1180x720+5", "x", None, 5])
def test_parse_geometry_rejects_junk(value):
    assert st.parse_geometry(value) is None


def test_clamp_keeps_a_geometry_that_fits():
    out = st.clamp_geometry({"width": 1180, "height": 720, "x": 120, "y": 80},
                            3840, 1080, min_width=900, min_height=560)
    assert out == "1180x720+120+80"


def test_clamp_caps_a_size_larger_than_the_screen():
    out = st.clamp_geometry({"width": 5000, "height": 2000}, 1920, 1080)
    assert out == "1920x1080"


def test_clamp_respects_the_window_minimum():
    out = st.clamp_geometry({"width": 200, "height": 100}, 1920, 1080,
                            min_width=900, min_height=560)
    assert out == "900x560"


def test_clamp_drops_a_position_that_is_off_screen():
    """
    This is the second-monitor case: a window saved at x=3800 on a dual-head
    desktop must not open off the edge of a single laptop screen.
    """
    out = st.clamp_geometry({"width": 1180, "height": 720, "x": 3800, "y": 80},
                            1920, 1080)
    assert out == "1180x720"


def test_clamp_drops_a_negative_vertical_position():
    out = st.clamp_geometry({"width": 1180, "height": 720, "x": 10, "y": -400},
                            1920, 1080)
    assert out == "1180x720"


def test_clamp_keeps_a_size_only_geometry_size_only():
    assert st.clamp_geometry({"width": 800, "height": 600}, 1920, 1080) == "800x600"
