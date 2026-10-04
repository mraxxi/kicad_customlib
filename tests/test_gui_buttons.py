"""
Every button is pressed at least once.

This file exists because two missing imports shipped. `import_dialog.py`
referenced `filepicker` and `settings as st` without importing either, so
"Add ZIP(s)…", "Add folder…" and "Add file(s)…" raised NameError the moment
they were clicked. Tk catches an exception inside a command callback, prints
it to the terminal and carries on, so from the interface the buttons simply
did nothing at all -- and the terminal is not where anyone is looking.

Nothing in the suite had ever invoked them: the widget tests constructed the
dialogs and checked their contents, which is exactly the shape of test that
cannot catch this. So the test here is deliberately shallow and deliberately
total: find every button, press it, and require that nothing raises.
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import pytest

from src.core import table_gen as tg
from src.gui import controller as ct


def _display_available() -> bool:
    try:
        root = tk.Tk()
    except Exception:  # noqa: BLE001
        return False
    root.destroy()
    return True


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
    A root that re-raises callback exceptions instead of swallowing them.

    This is the whole point of the file. Tk routes an exception raised inside
    a command callback to `report_callback_exception`, whose default prints a
    traceback to stderr and returns; `button.invoke()` then completes
    normally. So a test that presses a button and checks it "did not raise"
    passes even when the handler is broken -- which is how the missing
    imports survived a press-every-button test on the first attempt.
    """
    import gc

    root = tk.Tk()
    root.withdraw()
    root.callback_errors = []
    root.report_callback_exception = (
        lambda exc, value, tb: root.callback_errors.append(value)
    )
    yield root
    for window in root.winfo_children():
        try:
            window.destroy()
        except tk.TclError:
            pass
    gc.collect()
    root.destroy()
    gc.collect()


@pytest.fixture(autouse=True)
def no_real_dialogs(monkeypatch, tmp_path):
    """
    Stub out everything that would wait for a human or touch the system.

    The file pickers are stubbed at the `filepicker` module, not at
    `tkinter.filedialog`, precisely so that a call site which forgot to
    import `filepicker` still fails.
    """
    from src.gui import filepicker
    from src.gui import app as app_mod
    from src.gui import widgets as wd

    monkeypatch.setattr(filepicker, "open_files",
                        lambda *a, **k: k["on_done"]([]) if "on_done" in k else [])
    monkeypatch.setattr(filepicker, "open_directory",
                        lambda *a, **k: k["on_done"](None) if "on_done" in k else None)
    for module in (app_mod,):
        monkeypatch.setattr(module.messagebox, "showinfo", lambda *a, **k: None)
        monkeypatch.setattr(module.messagebox, "showerror", lambda *a, **k: None)
        monkeypatch.setattr(module.messagebox, "askyesno", lambda *a, **k: False)
    monkeypatch.setattr(wd.PlanPreview, "confirm", classmethod(lambda cls, *a, **k: False))
    monkeypatch.setattr(wd.ChoiceDialog, "ask", classmethod(lambda cls, *a, **k: "cancel"))
    # Keep every Toplevel the buttons might open from actually grabbing.
    monkeypatch.setattr(wd, "modal", lambda window, parent: None)
    # ...and from blocking: a handler that opens a dialog then waits for it
    # would never return, because nothing is going to close it.
    monkeypatch.setattr(tk.Misc, "wait_window", lambda self, window=None: None)
    # The audit shells out to kicad-cli; its content is tested elsewhere.
    from src.core import check as check_mod
    monkeypatch.setattr(check_mod, "run",
                        lambda root, **k: check_mod.Report(root=Path(root)))
    yield


def buttons_of(widget: tk.Misc):
    """Every ttk.Button in the tree, with its label."""
    found = []
    for child in widget.winfo_children():
        if isinstance(child, ttk.Button):
            found.append(child)
        found.extend(buttons_of(child))
    return found


def press_all(widget: tk.Misc, skip=()) -> list:
    """
    Press every enabled button, and fail on any exception Tk would have
    swallowed. See the `tk_root` docstring.
    """
    root = widget.winfo_toplevel()
    while not hasattr(root, "callback_errors") and root.master is not None:
        root = root.master.winfo_toplevel()
    errors = getattr(root, "callback_errors", None)
    before = len(errors) if errors is not None else 0

    pressed = []
    for button in buttons_of(widget):
        label = str(button.cget("text"))
        if label in skip or str(button.cget("state")) == "disabled":
            continue
        button.invoke()
        pressed.append(label)
        if errors is not None and len(errors) > before:
            raise AssertionError(f"pressing {label!r} raised {errors[-1]!r}")
    return pressed


@pytest.fixture
def control(populated_lib, monkeypatch):
    from src.core import check as check_mod
    tg.generate_tables(populated_lib)
    controller = ct.Controller(populated_lib)
    monkeypatch.setattr(controller, "audit",
                        lambda **k: check_mod.Report(root=populated_lib))
    return controller


def test_every_button_in_the_main_window_can_be_pressed(tk_root, control, monkeypatch):
    from src.gui.app import LibraryManagerApp

    app = LibraryManagerApp(tk_root, control.root)
    tk_root.update_idletasks()
    pressed = press_all(tk_root)
    assert pressed, "no buttons were found to press"
    assert "Import..." in pressed
    assert "Package project..." in pressed


def test_every_button_in_the_import_dialog_can_be_pressed(tk_root, control):
    """
    The regression this file was written for: all three source buttons raised
    NameError, and the interface showed nothing at all.
    """
    from src.gui.import_dialog import ImportDialog
    from src.gui import settings as st

    dialog = ImportDialog(tk_root, control, on_done=lambda _m: None,
                          settings=st.Settings())
    tk_root.update_idletasks()
    pressed = press_all(dialog)
    for label in ("Add ZIP(s)...", "Add folder...", "Add file(s)...", "Clear"):
        assert label in pressed, f"{label} was never pressed"
    dialog.destroy()


def test_the_import_dialog_source_buttons_reach_the_picker(tk_root, control, monkeypatch):
    """Not just "did not raise": the picker is actually called, with a start directory."""
    from src.gui import filepicker
    from src.gui.import_dialog import ImportDialog
    from src.gui import settings as st

    calls = []
    monkeypatch.setattr(filepicker, "open_files",
                        lambda *a, **k: calls.append(("files", k.get("initialdir"))))
    monkeypatch.setattr(filepicker, "open_directory",
                        lambda *a, **k: calls.append(("dir", k.get("initialdir"))))

    dialog = ImportDialog(tk_root, control, on_done=lambda _m: None,
                          settings=st.Settings())
    dialog._add_zips()
    dialog._add_folder()
    dialog._add_files()
    assert [kind for kind, _ in calls] == ["files", "dir", "files"]
    assert all(start is not None for _kind, start in calls)
    dialog.destroy()


def test_every_button_in_the_sync_view_can_be_pressed(tk_root, git_repo):
    from src.gui.sync_dialog import SyncDialog

    control = ct.Controller(git_repo)
    dialog = SyncDialog(tk_root, control)
    dialog._audit_errors = []
    dialog._busy = ""
    dialog.refresh()
    tk_root.update_idletasks()
    # Close would destroy the window mid-test; the rest are safe because the
    # repository is clean, so every git action is blocked and disabled.
    press_all(dialog, skip={"Close"})
    dialog.destroy()


def test_every_button_in_the_audit_dialog_can_be_pressed(tk_root, control):
    from src.gui.app import AuditDialog
    from src.gui.browser import LibraryBrowser

    browser = LibraryBrowser(tk_root, control)
    dialog = AuditDialog(tk_root, control, browser)
    dialog._run()
    tk_root.update_idletasks()
    press_all(dialog, skip={"Close"})
    dialog.destroy()


def test_every_button_in_the_rename_dialogs_can_be_pressed(tk_root, control):
    from src.gui.rename_dialog import CategoryRenameDialog, MoveDialog, RenameDialog

    row = control.rows(ct.KIND_SYMBOL)[0]
    for factory in (lambda: RenameDialog(tk_root, control, row),
                    lambda: MoveDialog(tk_root, control, row),
                    lambda: CategoryRenameDialog(tk_root, control, row.category)):
        dialog = factory()
        tk_root.update_idletasks()
        press_all(dialog, skip={"Cancel"})
        dialog.destroy()


def test_every_browser_button_can_be_pressed(tk_root, control):
    from src.gui.browser import LibraryBrowser

    browser = LibraryBrowser(tk_root, control)
    browser.pack(fill=tk.BOTH, expand=True)
    tk_root.update_idletasks()
    assert "Clear" in press_all(browser)


def test_every_menu_command_in_the_main_window_can_be_invoked(tk_root, control):
    """
    Menu entries are a second, separate set of callbacks, and the same class
    of mistake hides there just as well.
    """
    from src.gui.app import LibraryManagerApp

    app = LibraryManagerApp(tk_root, control.root)
    tk_root.update_idletasks()
    skip = {"Quit"}                   # it would close the window
    invoked = []
    for menu in (app.item_menu, app.category_menu):
        for index in range(menu.index("end") + 1):
            if menu.type(index) == "separator":
                continue
            label = str(menu.entrycget(index, "label"))
            if label in skip:
                continue
            menu.invoke(index)
            invoked.append(label)
    assert "Open in KiCad" in invoked
    assert "Rename..." in invoked
    assert tk_root.callback_errors == []
