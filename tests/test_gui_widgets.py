"""
Phase 3: widget smoke tests.

These only check that the interface can be constructed and driven, which is
what catches the class of bug that broke the old GUI -- a callback firing
before the widget it writes to exists, or a lookup that depends on displayed
text. Everything about *what* is shown is tested headlessly in
test_gui_controller.py.

The whole module skips when no display is available, so CI stays green.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")

from src.core import ops                      # noqa: E402
from src.core import table_gen as tg          # noqa: E402
from src.gui import controller as ct          # noqa: E402
from tests import kicad_fixtures as kf        # noqa: E402


def _display_available() -> bool:
    try:
        root = tk.Tk()
    except Exception:  # noqa: BLE001
        return False
    root.destroy()
    return True


# Tcl on the hosted Windows runner dies after roughly 34 interpreter
# create/destroy cycles ("Can't find a usable init.tcl"). That is a property of
# that image, not of Windows, so the skip is limited to CI and a developer on a
# real Windows desktop still gets the coverage.
#
# Worth knowing where this module actually runs: in CI only macOS exercises it,
# because the Ubuntu runner has no display and skips on that basis anyway. Its
# real coverage comes from macOS CI plus any developer machine with a display.
_WINDOWS_CI = sys.platform.startswith("win") and os.environ.get("CI") == "true"

pytestmark = [
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
    A fresh Tk interpreter per test.

    A session-scoped shared root was tried, to work around Tcl exhaustion on
    the Windows runner, and it hung every macOS job indefinitely -- most
    likely a grab left behind by a destroyed Toplevel, which macOS enforces
    and Linux tolerates. Per-test roots are the configuration that is known to
    pass on Linux and macOS, so Windows CI skips this module instead.

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


@pytest.fixture
def control(populated_lib):
    tg.generate_tables(populated_lib)
    return ct.Controller(populated_lib)


# --------------------------------------------------------------------------
# Shared widgets
# --------------------------------------------------------------------------

def test_status_bar_shows_messages(tk_root):
    from src.gui.widgets import StatusBar
    bar = StatusBar(tk_root)
    bar.set("done", detail="3 items")
    assert bar._var.get() == "done"
    assert bar._detail.get() == "3 items"


def test_category_picker_lists_only_real_categories(tk_root, control):
    """
    Creating a library is a deliberate act, so it lives on a button rather
    than as a magic entry you can reach by mis-clicking in the list.
    """
    from src.gui.widgets import CategoryPicker
    picker = CategoryPicker(tk_root, control.categories(),
                            validate=control.validate_new_category)
    assert list(picker._combo.cget("values")) == ["Amp_Test", "Conn_Test"]


def test_category_picker_offers_a_new_button(tk_root, control):
    from src.gui.widgets import NEW_CATEGORY_BUTTON, CategoryPicker
    picker = CategoryPicker(tk_root, control.categories(),
                            validate=control.validate_new_category)
    assert picker._new_button.cget("text") == NEW_CATEGORY_BUTTON


def test_category_picker_returns_empty_before_a_choice(tk_root, control):
    from src.gui.widgets import CategoryPicker
    picker = CategoryPicker(tk_root, control.categories(),
                            validate=control.validate_new_category)
    assert picker.get() == ""


def test_category_picker_round_trips_a_set_value(tk_root, control):
    from src.gui.widgets import CategoryPicker
    picker = CategoryPicker(tk_root, control.categories(),
                            validate=control.validate_new_category)
    picker.set("Amp_Test")
    assert picker.get() == "Amp_Test"


def test_plan_preview_defaults_to_not_applying(tk_root, control):
    """A closed window must never be read as consent."""
    from src.gui.widgets import PlanPreview
    plan = control.plan_rename(ct.KIND_SYMBOL, "Amp_Test", "TPA3255DDV", "NEW")
    dialog = PlanPreview(tk_root, plan)
    assert dialog.result is False
    dialog.destroy()


def test_plan_preview_shows_the_operations_and_warnings(tk_root, control):
    from src.gui.widgets import PlanPreview
    plan = control.plan_rename(ct.KIND_SYMBOL, "Amp_Test", "TPA3255DDV", "NEW")
    dialog = PlanPreview(tk_root, plan)
    shown = dialog.children
    text_widgets = [w for w in _walk(dialog) if isinstance(w, tk.Text)]
    body = text_widgets[0].get("1.0", tk.END)
    assert "TPA3255DDV" in body
    assert "will break" in body
    dialog.destroy()


def test_plan_preview_disables_apply_for_an_empty_plan(tk_root, control):
    from src.gui.widgets import PlanPreview
    empty = ops.Plan(root=control.root, title="nothing")
    dialog = PlanPreview(tk_root, empty)
    buttons = [w for w in _walk(dialog) if isinstance(w, type(dialog).__mro__[0])]
    assert dialog.result is False
    dialog.destroy()


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def test_sortable_tree_repopulate_keeps_selection(tk_root, control):
    from src.gui.widgets import SortableTree
    rows = control.rows(ct.KIND_SYMBOL)
    tree = SortableTree(tk_root, ct.COLUMNS[ct.KIND_SYMBOL])
    tree.repopulate(rows)
    tree.selection_set(rows[0].iid)
    tree.repopulate(rows)
    assert tree.selection() == (rows[0].iid,)


def test_sortable_tree_drops_a_selection_that_no_longer_exists(tk_root, control):
    from src.gui.widgets import SortableTree
    rows = control.rows(ct.KIND_SYMBOL)
    tree = SortableTree(tk_root, ct.COLUMNS[ct.KIND_SYMBOL])
    tree.repopulate(rows)
    tree.selection_set(rows[0].iid)
    tree.repopulate(rows[1:])
    assert tree.selection() == ()


def test_sortable_tree_sorts_on_a_column(tk_root, control):
    from src.gui.widgets import SortableTree
    tree = SortableTree(tk_root, ct.COLUMNS[ct.KIND_SYMBOL])
    tree.repopulate(control.rows(ct.KIND_SYMBOL))
    tree._sort_by("Name")
    names = [tree.set(iid, "Name") for iid in tree.get_children("")]
    assert names == sorted(names, key=str.lower)
    tree._sort_by("Name")
    names_desc = [tree.set(iid, "Name") for iid in tree.get_children("")]
    assert names_desc == sorted(names, key=str.lower, reverse=True)


# --------------------------------------------------------------------------
# Browser
# --------------------------------------------------------------------------

def test_browser_lists_categories_plus_an_all_entry(tk_root, control):
    from src.gui.browser import ALL_CATEGORIES, LibraryBrowser
    browser = LibraryBrowser(tk_root, control)
    assert browser.category_list.get(0) == ALL_CATEGORIES
    assert browser.category_list.size() == len(control.categories()) + 1


def test_browser_selecting_a_category_filters_the_items(tk_root, control):
    from src.gui.browser import LibraryBrowser
    browser = LibraryBrowser(tk_root, control)
    browser.category_list.selection_clear(0, tk.END)
    browser.category_list.selection_set(2)      # Conn_Test
    browser.refresh_items()
    assert browser.selected_category() == "Conn_Test"
    assert [browser.tree.set(i, "Name") for i in browser.tree.get_children("")] == [
        "KF2EDG"
    ]


def test_browser_switching_kind_changes_the_columns(tk_root, control):
    from src.gui.browser import LibraryBrowser
    browser = LibraryBrowser(tk_root, control)
    browser.kind.set(ct.KIND_FOOTPRINT)
    browser.refresh_items()
    assert tuple(browser.tree.cget("columns")) == ct.COLUMNS[ct.KIND_FOOTPRINT]


def test_browser_search_filters_live(tk_root, control):
    from src.gui.browser import LibraryBrowser
    browser = LibraryBrowser(tk_root, control)
    browser.search.set("KF2")
    assert [r.name for r in browser._rows] == ["KF2EDG"]


def test_browser_selected_rows_are_looked_up_by_iid_not_display_text(tk_root, control):
    from src.gui.browser import LibraryBrowser
    browser = LibraryBrowser(tk_root, control)
    first = browser._rows[0]
    browser.tree.selection_set(first.iid)
    selected = browser.selected_rows()
    assert len(selected) == 1
    assert selected[0].name == first.name
    assert isinstance(selected[0].category, str)


def test_browser_handles_a_numeric_category_name(tk_root, lib_root):
    """
    A category called '3255' is exactly what this repo accumulated, and it is
    what the old Treeview lookup turned into an int and then failed to find.
    """
    kf.write_symbol(lib_root / "symbols" / "3255.kicad_symdir" / "3255.kicad_sym")
    from src.gui.browser import LibraryBrowser
    control = ct.Controller(lib_root)
    browser = LibraryBrowser(tk_root, control)
    browser.category_list.selection_clear(0, tk.END)
    browser.category_list.selection_set(1)
    browser.refresh_items()
    assert browser.selected_category() == "3255"
    row = browser._rows[0]
    browser.tree.selection_set(row.iid)
    assert browser.selected_rows()[0].category == "3255"


def test_browser_column_widths_survive_a_kind_switch(tk_root, control, tmp_path):
    """
    The widths must come back when the user returns to a view, which is why
    only the last column stretches -- see SortableTree.set_columns.
    """
    from src.gui import settings as st
    from src.gui.browser import LibraryBrowser
    layout = st.LayoutStore(st.Settings(path=tmp_path / "gui.json"), "k")
    browser = LibraryBrowser(tk_root, control, layout=layout)

    browser.tree.column("Name", width=333)
    browser._remember_columns()
    browser.kind.set(ct.KIND_FOOTPRINT)
    browser.refresh_items()
    browser.kind.set(ct.KIND_SYMBOL)
    browser.refresh_items()
    assert browser.tree.column("Name", "width") == 333


def test_browser_column_widths_are_stored_per_kind(tk_root, control, tmp_path):
    from src.gui import settings as st
    from src.gui.browser import LibraryBrowser
    layout = st.LayoutStore(st.Settings(path=tmp_path / "gui.json"), "k")
    browser = LibraryBrowser(tk_root, control, layout=layout)

    browser.tree.column("Name", width=333)
    browser._remember_columns()
    browser.kind.set(ct.KIND_MODEL)
    browser.refresh_items()
    browser.tree.column("Name", width=111)
    browser._remember_columns()

    assert layout.columns(ct.KIND_SYMBOL)["Name"] == 333
    assert layout.columns(ct.KIND_MODEL)["Name"] == 111


def test_only_the_last_column_stretches(tk_root, control):
    """With stretch everywhere, Tk overrides restored widths on every resize."""
    from src.gui.browser import LibraryBrowser
    browser = LibraryBrowser(tk_root, control)
    columns = list(browser.tree.cget("columns"))
    stretches = [bool(browser.tree.column(c, "stretch")) for c in columns]
    assert stretches == [False] * (len(columns) - 1) + [True]


def test_browser_restores_the_remembered_view(tk_root, control, tmp_path):
    from src.gui import settings as st
    from src.gui.browser import LibraryBrowser
    settings = st.Settings(path=tmp_path / "gui.json")
    layout = st.LayoutStore(settings, "k")
    layout.remember_view(ct.KIND_FOOTPRINT, "Conn_Test")

    browser = LibraryBrowser(tk_root, control, layout=layout)
    browser.restore_view()
    assert browser.kind.get() == ct.KIND_FOOTPRINT
    assert browser.selected_category() == "Conn_Test"


def test_browser_restoring_a_vanished_category_falls_back_to_all(
    tk_root, control, tmp_path
):
    from src.gui import settings as st
    from src.gui.browser import LibraryBrowser
    layout = st.LayoutStore(st.Settings(path=tmp_path / "gui.json"), "k")
    layout.remember_view(ct.KIND_SYMBOL, "Deleted_Category")
    browser = LibraryBrowser(tk_root, control, layout=layout)
    browser.restore_view()
    assert browser.selected_category() is None


def test_browser_works_without_a_layout_store(tk_root, control):
    """Layout persistence is optional, so the widget stays usable standalone."""
    from src.gui.browser import LibraryBrowser
    browser = LibraryBrowser(tk_root, control, layout=None)
    browser.refresh_items()
    browser.remember_sort()
    browser._remember_columns()
    assert browser._rows


def test_browser_select_item_jumps_to_a_row(tk_root, control):
    from src.gui.browser import LibraryBrowser
    browser = LibraryBrowser(tk_root, control)
    assert browser.select_item(ct.KIND_FOOTPRINT, "Amp_Test", "SOP63P810X120-44N")
    assert browser.selected_row().name == "SOP63P810X120-44N"


def test_browser_select_item_returns_false_for_something_absent(tk_root, control):
    from src.gui.browser import LibraryBrowser
    browser = LibraryBrowser(tk_root, control)
    assert browser.select_item(ct.KIND_SYMBOL, "Amp_Test", "NOPE") is False


# --------------------------------------------------------------------------
# App shell
# --------------------------------------------------------------------------

def test_app_constructs_and_refreshes(tk_root, control):
    from src.gui.app import LibraryManagerApp
    app = LibraryManagerApp(tk_root, control.root)
    app.refresh()
    assert app.browser.category_list.size() == 3


def test_app_constructs_against_an_empty_library(tk_root, lib_root):
    from src.gui.app import LibraryManagerApp
    app = LibraryManagerApp(tk_root, lib_root)
    assert app.browser.category_list.size() == 1      # just [All categories]


def test_app_restores_a_saved_window_size_clamped_to_the_screen(
    tk_root, control, monkeypatch, tmp_path
):
    """A size saved on a bigger desktop must be capped, not applied blindly."""
    from src.gui import settings as st
    from src.gui.app import LibraryManagerApp

    monkeypatch.setattr(st.Settings, "load",
                        classmethod(lambda cls, path=None: cls(path=tmp_path / "gui.json")))
    seeded = st.Settings(path=tmp_path / "gui.json")
    key = st.Settings.profile_key(tk_root)
    seeded.set_window_geometry(key, st.WINDOW_MAIN, "99999x99999+0+0")
    monkeypatch.setattr(st.Settings, "load", classmethod(lambda cls, path=None: seeded))

    applied = []
    real_geometry = tk_root.geometry
    monkeypatch.setattr(tk_root, "geometry",
                        lambda *a: (applied.append(a[0]) or None) if a else real_geometry())
    LibraryManagerApp(tk_root, control.root)
    assert applied, "no geometry was applied"
    parsed = st.parse_geometry(applied[0])
    assert parsed["width"] <= tk_root.winfo_screenwidth()
    assert parsed["height"] <= tk_root.winfo_screenheight()


def test_app_falls_back_to_the_default_size_for_junk_geometry(
    tk_root, control, monkeypatch, tmp_path
):
    from src.gui import settings as st
    from src.gui.app import LibraryManagerApp

    seeded = st.Settings(path=tmp_path / "gui.json")
    seeded.set_window_geometry(st.Settings.profile_key(tk_root), st.WINDOW_MAIN, "nonsense")
    monkeypatch.setattr(st.Settings, "load", classmethod(lambda cls, path=None: seeded))

    applied = []
    real_geometry = tk_root.geometry
    monkeypatch.setattr(tk_root, "geometry",
                        lambda *a: (applied.append(a[0]) or None) if a else real_geometry())
    LibraryManagerApp(tk_root, control.root)
    assert applied[0] == "1180x720"


def test_app_does_not_store_the_geometry_of_an_unmapped_window(tk_root, control):
    """
    A withdrawn root reports "1x1+0+0"; storing that would reopen the app as a
    one-pixel window.
    """
    from src.gui.app import LibraryManagerApp
    app = LibraryManagerApp(tk_root, control.root)       # tk_root is withdrawn
    assert app._geometry_is_storable() is False
    app._capture_layout()
    assert app.layout.window("main") is None


def test_app_debounces_the_layout_save(tk_root, control, monkeypatch, tmp_path):
    from src.gui import settings as st
    from src.gui.app import LibraryManagerApp
    seeded = st.Settings(path=tmp_path / "gui.json")
    monkeypatch.setattr(st.Settings, "load", classmethod(lambda cls, path=None: seeded))
    app = LibraryManagerApp(tk_root, control.root)

    saves = []
    monkeypatch.setattr(app.settings, "save", lambda: saves.append(1) or True)
    for _ in range(20):
        app._schedule_save()
    assert saves == [], "writing on every event would thrash the file"
    app._flush_save()
    assert len(saves) == 1


def test_reset_layout_clears_profiles_but_keeps_the_category(
    tk_root, control, monkeypatch, tmp_path
):
    from src.gui import settings as st
    from src.gui.app import LibraryManagerApp
    seeded = st.Settings(path=tmp_path / "gui.json")
    monkeypatch.setattr(st.Settings, "load", classmethod(lambda cls, path=None: seeded))
    app = LibraryManagerApp(tk_root, control.root)
    app.settings.set("last_category", "Keep_Me")
    app.layout.remember_sash(321)
    monkeypatch.setattr("src.gui.app.messagebox.askyesno", lambda *a, **k: True)

    app.on_reset_layout()
    assert app.settings.data["geometry"] == {}
    assert app.settings.get("last_category") == "Keep_Me"


def test_reset_layout_can_be_declined(tk_root, control, monkeypatch, tmp_path):
    from src.gui import settings as st
    from src.gui.app import LibraryManagerApp
    seeded = st.Settings(path=tmp_path / "gui.json")
    monkeypatch.setattr(st.Settings, "load", classmethod(lambda cls, path=None: seeded))
    app = LibraryManagerApp(tk_root, control.root)
    app.layout.remember_sash(321)
    monkeypatch.setattr("src.gui.app.messagebox.askyesno", lambda *a, **k: False)
    app.on_reset_layout()
    assert app.layout.sash() == 321


def test_app_shows_the_stale_banner_without_a_pack_error(tk_root, lib_root):
    """
    The banner must sit directly below the toolbar when the tables are stale.

    refresh() used to anchor it with `after=winfo_children()[0]`. That was
    correct in practice -- the toolbar is the first widget created with the
    root as parent -- but it broke the moment anything else occupied that
    slot, which is what a shared test interpreter did. Anchoring to an
    explicit reference removes the dependency on creation order.
    """
    from src.gui.app import LibraryManagerApp
    kf.write_symbol(lib_root / "symbols" / "C.kicad_symdir" / "S.kicad_sym")
    app = LibraryManagerApp(tk_root, lib_root)          # tables are stale
    assert app.controller.tables_are_stale
    app.refresh()                                        # must not raise
    assert app.banner.winfo_manager() == "pack"
    # Tk resolves `after` into pack order rather than reporting it back, so
    # assert the ordering: the banner sits immediately below the toolbar.
    order = tk_root.pack_slaves()
    assert order.index(app.banner) == order.index(app.toolbar) + 1


def test_app_hides_the_stale_banner_once_the_tables_are_current(tk_root, control):
    from src.gui.app import LibraryManagerApp
    app = LibraryManagerApp(tk_root, control.root)
    assert app.controller.tables_are_stale is False
    app.refresh()
    assert app.banner.winfo_manager() == ""


def test_app_can_be_constructed_twice_in_one_interpreter(tk_root, control):
    """Building a second app over the same root must not break refresh()."""
    from src.gui.app import LibraryManagerApp
    LibraryManagerApp(tk_root, control.root)
    second = LibraryManagerApp(tk_root, control.root)
    second.refresh()
    assert second.browser.category_list.size() == 3


def test_app_shows_details_for_a_selected_row(tk_root, control):
    from src.gui.app import LibraryManagerApp
    app = LibraryManagerApp(tk_root, control.root)
    row = next(r for r in app.browser._rows if r.name == "TPA3255DDV")
    app.browser.tree.selection_set(row.iid)
    # <<TreeviewSelect>> is a virtual event: it is dispatched by the event
    # loop, not synchronously by selection_set.
    tk_root.update()
    text = app.details.get("1.0", tk.END)
    assert "TPA3255DDV" in text
    assert "Amp_Test:SOP63P810X120-44N" in text


def test_app_clears_details_when_nothing_is_selected(tk_root, control):
    from src.gui.app import LibraryManagerApp
    app = LibraryManagerApp(tk_root, control.root)
    app.on_row_selected(None)
    assert app.details.get("1.0", tk.END).strip() == ""


def test_app_generate_is_a_no_op_when_tables_are_current(tk_root, control):
    from src.gui.app import LibraryManagerApp
    app = LibraryManagerApp(tk_root, control.root)
    app.on_generate()
    assert app.status._var.get() == "Tables are already up to date."


def test_import_dialog_constructs_with_no_source(tk_root, control):
    from src.gui.import_dialog import ImportDialog
    dialog = ImportDialog(tk_root, control)
    assert dialog.candidates == []
    assert str(dialog.import_button.cget("state")) == "disabled"
    dialog.destroy()


def test_import_dialog_lists_candidates_from_a_zip(tk_root, control, tmp_path):
    from src.gui.import_dialog import ImportDialog
    z = kf.zip_multi_part(tmp_path / "parts.zip")
    dialog = ImportDialog(tk_root, control, initial_category="Amp_Test")
    dialog._add_sources([z])
    try:
        assert len(dialog.candidates) == 9
        assert str(dialog.import_button.cget("state")) == "normal"
        assert len(dialog.tree.get_children("")) == 9
    finally:
        dialog._close()


def test_import_dialog_splits_a_multi_symbol_bundle_into_rows(tk_root, control, tmp_path):
    from src.gui.import_dialog import ImportDialog
    z = kf.zip_multi_symbol_cache(tmp_path / "cache.zip")
    dialog = ImportDialog(tk_root, control, initial_category="Amp_Test")
    dialog._add_sources([z])
    try:
        symbol_rows = [c for c in dialog.candidates if c.kind == "symbol"]
        assert len(symbol_rows) == 5
        sanitized = [c for c in symbol_rows if c.target_name != c.detected_name]
        assert len(sanitized) == 2        # the '*' and '(' names
    finally:
        dialog._close()


def test_import_dialog_toggling_a_row_disables_import_when_nothing_is_left(
    tk_root, control, tmp_path
):
    from src.gui.import_dialog import ImportDialog
    mod = kf.write_footprint(tmp_path / "FP.kicad_mod")
    dialog = ImportDialog(tk_root, control, initial_category="Amp_Test")
    dialog._add_sources([mod])
    try:
        assert str(dialog.import_button.cget("state")) == "normal"
        dialog.tree.selection_set(dialog.candidates[0].key)
        dialog._toggle_selected()
        assert str(dialog.import_button.cget("state")) == "disabled"
    finally:
        dialog._close()


def test_import_dialog_accepts_a_folder_source(tk_root, control, tmp_path):
    """The old dialog used askopenfilename, which cannot select a folder."""
    from src.gui.import_dialog import ImportDialog
    src = tmp_path / "part"
    kf.write_symbol(src / "NE555.kicad_sym", footprint="DIP8")
    kf.write_footprint(src / "DIP8.kicad_mod")
    dialog = ImportDialog(tk_root, control, initial_category="Amp_Test")
    dialog._add_sources([src])
    try:
        assert {c.kind for c in dialog.candidates} == {"symbol", "footprint"}
    finally:
        dialog._close()


def test_rename_dialog_builds_a_live_plan(tk_root, control):
    from src.gui.rename_dialog import RenameDialog
    row = next(r for r in control.rows(ct.KIND_SYMBOL) if r.name == "TPA3255DDV")
    dialog = RenameDialog(tk_root, control, row)
    try:
        dialog.new_name.set("TPA3255DDV_V2")
        dialog._rebuild()
        assert dialog._plan is not None and not dialog._plan.is_empty
        assert str(dialog.apply_button.cget("state")) == "normal"
        body = dialog.preview.get("1.0", tk.END)
        assert "TPA3255DDV_V2" in body
    finally:
        dialog.destroy()


def test_rename_dialog_disables_apply_for_an_unchanged_name(tk_root, control):
    from src.gui.rename_dialog import RenameDialog
    row = next(r for r in control.rows(ct.KIND_SYMBOL) if r.name == "TPA3255DDV")
    dialog = RenameDialog(tk_root, control, row)
    try:
        assert str(dialog.apply_button.cget("state")) == "disabled"
    finally:
        dialog.destroy()


def test_rename_dialog_reports_an_invalid_name_without_crashing(tk_root, control):
    from src.gui.rename_dialog import RenameDialog
    row = next(r for r in control.rows(ct.KIND_SYMBOL) if r.name == "TPA3255DDV")
    dialog = RenameDialog(tk_root, control, row)
    try:
        dialog.new_name.set("Bad:Name")
        dialog._rebuild()
        assert "lib_id" in dialog.feedback.cget("text")
        assert str(dialog.apply_button.cget("state")) == "disabled"
    finally:
        dialog.destroy()


def test_move_dialog_excludes_the_current_category(tk_root, control):
    from src.gui.rename_dialog import MoveDialog
    row = next(r for r in control.rows(ct.KIND_SYMBOL) if r.name == "KF2EDG")
    dialog = MoveDialog(tk_root, control, row)
    try:
        values = list(dialog.picker._combo.cget("values"))
        assert "Conn_Test" not in values
        assert "Amp_Test" in values
    finally:
        dialog.destroy()


def test_category_rename_dialog_builds_a_plan(tk_root, control):
    from src.gui.rename_dialog import CategoryRenameDialog
    dialog = CategoryRenameDialog(tk_root, control, "Amp_Test")
    try:
        dialog.new_name.set("TI_Amps")
        dialog._rebuild()
        assert dialog._plan is not None
        assert "library nickname changes" in dialog.preview.get("1.0", tk.END)
    finally:
        dialog.destroy()


def test_audit_dialog_lists_findings(tk_root, lib_root):
    from src.gui.app import AuditDialog
    from src.gui.browser import LibraryBrowser
    kf.write_symbol(
        lib_root / "symbols" / "C.kicad_symdir" / "S.kicad_sym", footprint="C:GONE"
    )
    control = ct.Controller(lib_root)
    browser = LibraryBrowser(tk_root, control)
    dialog = AuditDialog(tk_root, control, browser)
    try:
        dialog._run()
        rows = dialog.tree.get_children("")
        assert rows
        codes = {dialog.tree.set(r, "Code") for r in rows}
        assert "footprint-ref-broken" in codes
    finally:
        dialog.destroy()


def test_audit_dialog_filters_by_severity(tk_root, lib_root):
    from src.gui.app import AuditDialog
    from src.gui.browser import LibraryBrowser
    from src.core import check as check_mod
    (lib_root / "symbols" / "Empty.kicad_symdir").mkdir()
    tg.generate_tables(lib_root)
    control = ct.Controller(lib_root)
    dialog = AuditDialog(tk_root, control, LibraryBrowser(tk_root, control))
    try:
        dialog._run()
        dialog.severity.set(check_mod.ERROR)
        dialog._repopulate()
        assert dialog.tree.get_children("") == ()
        dialog.severity.set(check_mod.WARNING)
        dialog._repopulate()
        assert dialog.tree.get_children("")
    finally:
        dialog.destroy()
