"""
Phase 5: the polish items.

Each is small and independent, so the tests are too. The headless half covers
the empty-state wording, which is a Controller decision; the Tk half covers
the quit confirmation, the placeholder swap, "Open in KiCad" and the audit
filter surviving a reopen.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from src.core import table_gen as tg
from src.gui import controller as ct
from src.gui import widgets as wd

tk = pytest.importorskip("tkinter")


# --------------------------------------------------------------------------
# Empty-state wording: headless
# --------------------------------------------------------------------------

@pytest.fixture
def empty(lib_root):
    return ct.Controller(lib_root)


@pytest.fixture
def stocked(populated_lib):
    tg.generate_tables(populated_lib)
    return ct.Controller(populated_lib)


def test_an_empty_library_says_where_to_start(empty):
    """
    A blank table is the worst possible first impression: it looks identical
    to a broken one and gives the user nowhere to go.
    """
    message = empty.empty_state_message(ct.KIND_SYMBOL)
    assert "This library is empty" in message
    assert "Import" in message
    assert os.path.join("staging-temp", "intake") in message


def test_an_empty_category_says_that_is_normal(stocked):
    message = stocked.empty_state_message(ct.KIND_MODEL, category="Conn_Test")
    assert "Conn_Test holds no 3D models" in message
    assert "normal" in message


def test_a_search_with_no_matches_quotes_the_search(stocked):
    message = stocked.empty_state_message(ct.KIND_SYMBOL, search="  zzz  ")
    assert "zzz" in message
    assert "matches" in message


def test_a_search_within_a_category_names_the_category(stocked):
    message = stocked.empty_state_message(
        ct.KIND_SYMBOL, category="Conn_Test", search="zzz")
    assert "in Conn_Test" in message


def test_a_populated_library_with_no_items_of_one_kind(stocked):
    message = stocked.empty_state_message(ct.KIND_MODEL)
    assert "no 3D models" in message


# --------------------------------------------------------------------------
# The spacing grid
# --------------------------------------------------------------------------

def test_the_spacing_constants_are_on_an_eight_pixel_grid():
    """
    One grid, defined once. Before this every new widget was a fresh guess at
    a padding, and nothing lined up between panels.
    """
    assert (wd.SPACE_XS, wd.SPACE_S, wd.SPACE_M) == (4, 8, 16)
    for value in (wd.PAD_DIALOG, wd.PAD_SECTION, wd.GAP, wd.GAP_M):
        assert value % wd.SPACE_XS == 0
    assert wd.PAD_BAR == (wd.SPACE_S, wd.SPACE_XS)


@pytest.mark.parametrize("module", [
    "app.py", "browser.py", "widgets.py", "sync_dialog.py",
    "import_dialog.py", "rename_dialog.py",
])
def test_no_module_hardcodes_a_padding(module):
    """
    A literal 0 is fine -- it means "no padding on that side", not a size
    choice. Any other number is a guess that belongs on the grid.
    """
    import re
    source = (Path(ct.__file__).parent / module).read_text(encoding="utf-8")
    offenders = re.findall(r"(?:padding|pady|padx)=\(?\s*[1-9]\d*", source)
    assert offenders == [], offenders


# --------------------------------------------------------------------------
# Tk
# --------------------------------------------------------------------------

def _display_available() -> bool:
    try:
        root = tk.Tk()
    except Exception:  # noqa: BLE001
        return False
    root.destroy()
    return True


_WINDOWS_CI = sys.platform.startswith("win") and os.environ.get("CI") == "true"

needs_tk = [
    pytest.mark.skipif(not _display_available(), reason="no display available for Tk"),
    pytest.mark.skipif(
        _WINDOWS_CI,
        reason="Tcl on the Windows CI image fails after many interpreter "
               "create/destroy cycles; exercised by macOS CI and local runs",
    ),
]


@pytest.fixture
def tk_root():
    """
    The gc.collect() before destroy() is not superstition. tkinter.Variable
    defines __del__, which unsets the Tcl variable; if one is still waiting
    to be collected when the interpreter goes away, that __del__ raises
    "main thread is not in main loop" later, during whichever unrelated test
    happens to trigger the collection. Collecting here keeps a leak from one
    Tk test from being reported as a failure in another file.
    """
    import gc

    root = tk.Tk()
    root.withdraw()
    yield root
    for window in root.winfo_children():
        try:
            window.destroy()
        except tk.TclError:
            pass
    gc.collect()
    root.destroy()
    gc.collect()


class TestEmptyState:
    pytestmark = needs_tk

    def test_the_placeholder_replaces_the_table_and_gives_it_back(
        self, tk_root, populated_lib
    ):
        from src.gui.browser import LibraryBrowser
        tg.generate_tables(populated_lib)
        control = ct.Controller(populated_lib)
        browser = LibraryBrowser(tk_root, control)
        browser.pack(fill=tk.BOTH, expand=True)
        tk_root.update_idletasks()
        assert browser.tree in browser.table_holder.pack_slaves()
        assert browser.empty_state_text == ""

        browser.search.set("nothing-matches-this")
        tk_root.update_idletasks()
        assert not browser.tree.winfo_ismapped()
        assert "nothing-matches-this" in browser.empty_state_text

        browser.search.set("")
        tk_root.update_idletasks()
        assert browser.tree in browser.table_holder.pack_slaves()
        assert browser.empty_state_text == ""

    def test_an_empty_library_opens_on_the_placeholder(self, tk_root, lib_root):
        from src.gui.browser import LibraryBrowser
        browser = LibraryBrowser(tk_root, ct.Controller(lib_root))
        browser.pack(fill=tk.BOTH, expand=True)
        tk_root.update_idletasks()
        assert "This library is empty" in browser.empty_state_text


class TestChoiceDialog:
    pytestmark = needs_tk

    def test_a_pressed_button_is_reported_by_key(self, tk_root):
        dialog = wd.ChoiceDialog(tk_root, "T", "message",
                                 (("a", "Alpha"), ("b", "Beta")))
        try:
            dialog._pick("b")
            assert dialog.choice == "b"
        finally:
            if dialog.winfo_exists():
                dialog.destroy()

    def test_closing_the_window_is_not_an_answer(self, tk_root):
        """Dismissing a three-way question must never pick one of the three."""
        dialog = wd.ChoiceDialog(tk_root, "T", "message",
                                 (("a", "Alpha"), ("b", "Beta")))
        dialog.destroy()
        assert dialog.choice is None


class TestQuitConfirmation:
    pytestmark = needs_tk

    def _app(self, tk_root, root_dir):
        from src.gui.app import LibraryManagerApp
        return LibraryManagerApp(tk_root, root_dir)

    def test_a_clean_repo_quits_without_asking(self, tk_root, git_repo, monkeypatch):
        from src.gui import app as app_mod
        asked = []
        monkeypatch.setattr(app_mod.ChoiceDialog, "ask",
                            classmethod(lambda cls, *a, **k: asked.append(1)))
        app = self._app(tk_root, git_repo)
        assert app._confirm_quit() is True
        assert asked == []

    def test_a_non_repo_quits_without_asking(self, tk_root, lib_root, monkeypatch):
        from src.gui import app as app_mod
        asked = []
        monkeypatch.setattr(app_mod.ChoiceDialog, "ask",
                            classmethod(lambda cls, *a, **k: asked.append(1)))
        app = self._app(tk_root, lib_root)
        assert app._confirm_quit() is True
        assert asked == []

    def test_uncommitted_changes_prompt_and_quit_anyway_wins(
        self, tk_root, git_repo, monkeypatch
    ):
        from src.gui import app as app_mod
        from tests.conftest import write_file
        write_file(git_repo, "symbols/A.kicad_symdir/X.kicad_sym", "(symbol)\n")
        shown = {}
        monkeypatch.setattr(
            app_mod.ChoiceDialog, "ask",
            classmethod(lambda cls, parent, title, message, choices, **k:
                        shown.update(message=message, choices=choices) or "quit"))
        app = self._app(tk_root, git_repo)
        assert app._confirm_quit() is True
        assert "other machine" in shown["message"]
        assert [k for k, _ in shown["choices"]] == ["sync", "quit", "cancel"]

    def test_cancel_keeps_the_window_open(self, tk_root, git_repo, monkeypatch):
        from src.gui import app as app_mod
        from tests.conftest import write_file
        write_file(git_repo, "a.txt", "x\n")
        monkeypatch.setattr(app_mod.ChoiceDialog, "ask",
                            classmethod(lambda cls, *a, **k: "cancel"))
        app = self._app(tk_root, git_repo)
        assert app._confirm_quit() is False

    def test_a_dismissed_prompt_keeps_the_window_open(
        self, tk_root, git_repo, monkeypatch
    ):
        """Quitting is never the fallback for an ambiguous answer."""
        from src.gui import app as app_mod
        from tests.conftest import write_file
        write_file(git_repo, "a.txt", "x\n")
        monkeypatch.setattr(app_mod.ChoiceDialog, "ask",
                            classmethod(lambda cls, *a, **k: None))
        app = self._app(tk_root, git_repo)
        assert app._confirm_quit() is False

    def test_commit_and_push_opens_the_sync_view(self, tk_root, git_repo, monkeypatch):
        from src.gui import app as app_mod
        from tests.conftest import write_file
        write_file(git_repo, "a.txt", "x\n")
        monkeypatch.setattr(app_mod.ChoiceDialog, "ask",
                            classmethod(lambda cls, *a, **k: "sync"))
        opened = []
        app = self._app(tk_root, git_repo)
        monkeypatch.setattr(app, "on_sync", lambda: opened.append(1))
        assert app._confirm_quit() is False     # the window stays open
        assert opened == [1]


class TestOpenInKicad:
    pytestmark = needs_tk

    def test_it_hands_the_file_to_the_desktop(self, tk_root, populated_lib, monkeypatch):
        from src.gui import app as app_mod
        tg.generate_tables(populated_lib)
        app = app_mod.LibraryManagerApp(tk_root, populated_lib)
        row = app.controller.rows(ct.KIND_SYMBOL)[0]
        monkeypatch.setattr(app.browser, "selected_row", lambda: row)
        launched = []
        monkeypatch.setattr(app_mod.subprocess, "Popen",
                            lambda argv, *a, **k: launched.append(argv))
        app.on_open_in_kicad()
        assert launched, "nothing was launched"
        assert str(row.path) in launched[0]

    def test_a_failure_goes_to_the_status_bar_not_a_dialog(
        self, tk_root, populated_lib, monkeypatch
    ):
        """
        Best-effort by design: there is no reliable way to know whether KiCad
        is installed, and failing to open a file does not deserve a dialog.
        """
        from src.gui import app as app_mod
        tg.generate_tables(populated_lib)
        app = app_mod.LibraryManagerApp(tk_root, populated_lib)
        row = app.controller.rows(ct.KIND_SYMBOL)[0]
        monkeypatch.setattr(app.browser, "selected_row", lambda: row)

        def boom(*a, **k):
            raise OSError("xdg-open: not found")
        monkeypatch.setattr(app_mod.subprocess, "Popen", boom)
        shown = []
        monkeypatch.setattr(app_mod.messagebox, "showerror",
                            lambda *a, **k: shown.append(a))
        app.on_open_in_kicad()
        assert shown == []


class TestAuditFilter:
    pytestmark = needs_tk

    def test_the_severity_filter_survives_closing_and_reopening(
        self, tk_root, populated_lib, monkeypatch
    ):
        from src.core import check as check_mod
        from src.gui import app as app_mod
        tg.generate_tables(populated_lib)
        app = app_mod.LibraryManagerApp(tk_root, populated_lib)
        assert app.audit_severity == "all"

        opened = []

        def fake_dialog(parent, controller, browser, *, severity="all"):
            dialog = tk.Toplevel(parent)
            dialog.severity = tk.StringVar(value=severity)
            opened.append(dialog)
            return dialog

        monkeypatch.setattr(app_mod, "AuditDialog", fake_dialog)
        monkeypatch.setattr(app.root, "wait_window", lambda _w: None)

        app.on_audit()
        opened[-1].severity.set(check_mod.ERROR)
        app.audit_severity = opened[-1].severity.get()
        opened[-1].destroy()

        app.on_audit()
        assert opened[-1].severity.get() == check_mod.ERROR
        opened[-1].destroy()

    def test_the_real_dialog_accepts_a_starting_severity(
        self, tk_root, populated_lib, monkeypatch
    ):
        from src.core import check as check_mod
        from src.gui.app import AuditDialog
        from src.gui.browser import LibraryBrowser
        tg.generate_tables(populated_lib)
        control = ct.Controller(populated_lib)
        browser = LibraryBrowser(tk_root, control)
        dialog = AuditDialog(tk_root, control, browser, severity=check_mod.WARNING)
        try:
            assert dialog.severity.get() == check_mod.WARNING
        finally:
            dialog.destroy()
