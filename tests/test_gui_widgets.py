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


pytestmark = pytest.mark.skipif(
    not _display_available(), reason="no display available for Tk"
)


@pytest.fixture(scope="session")
def _tk_session():
    """
    One Tk interpreter for the whole session.

    Creating and destroying one per test exhausted Tcl on a Windows CI runner:
    34 tests passed and the 35th failed with "Can't find a usable init.tcl".
    One interpreter, reused, is both more robust and faster.
    """
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


def _clear(root) -> None:
    """Return the shared interpreter to a clean slate between tests."""
    for child in list(root.winfo_children()):
        child.destroy()
    # LibraryManagerApp installs a menubar on the root. Clear it with an
    # empty string, which detaches it without creating another child widget.
    try:
        root.config(menu="")
    except tk.TclError:
        pass


@pytest.fixture
def tk_root(_tk_session):
    _clear(_tk_session)
    yield _tk_session
    _clear(_tk_session)


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


def test_category_picker_lists_categories_and_a_create_entry(tk_root, control):
    from src.gui.widgets import NEW_CATEGORY_SENTINEL, CategoryPicker
    picker = CategoryPicker(tk_root, control.categories(),
                            validate=control.validate_new_category)
    values = list(picker._combo.cget("values"))
    assert values[:-1] == ["Amp_Test", "Conn_Test"]
    assert values[-1] == NEW_CATEGORY_SENTINEL


def test_category_picker_never_returns_the_sentinel_as_a_name(tk_root, control):
    from src.gui.widgets import NEW_CATEGORY_SENTINEL, CategoryPicker
    picker = CategoryPicker(tk_root, control.categories(),
                            validate=control.validate_new_category)
    picker._value.set(NEW_CATEGORY_SENTINEL)
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


def test_app_shows_the_stale_banner_without_a_pack_error(tk_root, lib_root):
    """
    Regression: refresh() packed the banner `after=winfo_children()[0]`, which
    assumed the toolbar was the root's first child. Once a menubar or anything
    else took that slot, Tk raised "window ... isn't packed". The banner is now
    anchored to an explicit reference.
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
    """The shared Tk session reuses one interpreter, so this must be safe."""
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
