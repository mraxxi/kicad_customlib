"""
Phase 4: the git UI.

Two halves. The first is headless and tests the Controller's git layer --
caching, the summary line, the title suffix, what blocks a push. The second
needs Tk and checks that the strip and the sync view construct, that every
disabled action carries its reason, and that the override checkbox is what
gates the push.

Both halves use the repository fixtures from conftest.py, so nothing here
touches the network or the real library.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from src.core import check as check_mod
from src.core import vcs
from src.gui import controller as ct
from tests.conftest import commit_in, git, write_file as write

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git is not installed")


@pytest.fixture
def control(git_repo):
    return ct.Controller(git_repo)


# --------------------------------------------------------------------------
# Controller: headless
# --------------------------------------------------------------------------

def test_the_status_is_cached_until_asked_to_refresh(control, monkeypatch):
    """
    The strip reads this on every repaint, so a cache is not an optimisation
    so much as the difference between a usable interface and one that shells
    out to git continuously.
    """
    calls = []
    real = vcs.read_status
    monkeypatch.setattr(vcs, "read_status",
                        lambda root: calls.append(root) or real(root))
    first = control.git_status(refresh=True)
    assert len(calls) == 1
    assert control.git_status() is first
    assert len(calls) == 1
    assert control.git_status(refresh=True) is not first
    assert len(calls) == 2


def test_a_library_refresh_invalidates_the_git_cache(control, monkeypatch):
    """
    Anything that rescans the disk may well have changed the repository too,
    so holding on to the old status would show a stale strip.
    """
    control.git_status(refresh=True)
    write(control.root, "symbols/A.kicad_symdir/X.kicad_sym", "(symbol)\n")
    control.refresh()
    assert control.git_status().dirty is True


def test_summary_line_for_a_clean_repo(control):
    line = control.git_summary_line()
    assert line.startswith("main · ")
    assert "↑0 ↓0" in line
    assert "clean" in line


def test_summary_line_is_empty_outside_a_repo(lib_root):
    assert ct.Controller(lib_root).git_summary_line() == ""
    assert ct.Controller(lib_root).is_git_repo is False


def test_title_suffix_is_omitted_when_in_sync(control):
    """A title that always carries a git fragment stops being a signal."""
    assert control.git_title_suffix() == ""


def test_title_suffix_reports_ahead_and_modified(control):
    commit_in(control.root, "a.txt", "local")
    write(control.root, "b.txt")
    control.refresh()
    assert control.git_title_suffix() == "↑1 modified"


def test_title_suffix_reports_behind(control, second):
    commit_in(second, "theirs.txt", "theirs")
    git(second, "push")
    control.git_fetch()
    assert control.git_title_suffix() == "↓1"


def test_title_suffix_names_a_state_with_no_counts(control):
    git(control.root, "checkout", "-b", "side")
    control.refresh()
    assert control.git_title_suffix() == "no upstream branch"


def test_title_suffix_is_empty_outside_a_repo(lib_root):
    assert ct.Controller(lib_root).git_title_suffix() == ""


def test_incoming_and_outgoing_round_trip(control, second):
    commit_in(second, "theirs.txt", "from the other machine")
    git(second, "push")
    commit_in(control.root, "ours.txt", "from this machine")
    control.git_fetch()
    assert [c.subject for c in control.git_incoming()] == ["from the other machine"]
    assert [c.subject for c in control.git_outgoing()] == ["from this machine"]


def test_preview_commit_uses_the_generated_message(control):
    write(control.root, "symbols/Conn_XT.kicad_symdir/XT60.kicad_sym", "(symbol)\n")
    preview = control.git_preview_commit()
    assert preview.message == "Add 1 symbol to Conn_XT"
    assert ("?", "symbols/Conn_XT.kicad_symdir/XT60.kicad_sym") in preview.files


def test_preview_commit_honours_an_overridden_message(control):
    write(control.root, "a.txt")
    assert control.git_preview_commit("Mine").message == "Mine"


def test_commit_and_push_through_the_controller(control):
    write(control.root, "symbols/Conn_XT.kicad_symdir/XT60.kicad_sym", "(symbol)\n")
    committed = control.git_commit(control.git_suggested_message())
    assert committed.ok, committed.transcript()
    assert control.git_status().ahead == 1
    pushed = control.git_push(skip_check=True)
    assert pushed.ok, pushed.transcript()
    assert control.git_status().ahead == 0


def test_pull_rescans_the_library(control, second):
    """
    The point of the whole feature: what arrives from the other machine is
    new parts, and the browser must not keep showing the library as it was.
    """
    (second / "symbols" / "Conn_XT.kicad_symdir").mkdir(parents=True)
    (second / "symbols" / "Conn_XT.kicad_symdir" / "XT60.kicad_sym").write_text(
        '(kicad_symbol_lib (version 20241209) (symbol "XT60"))\n'
    )
    git(second, "add", "-A")
    git(second, "commit", "-m", "Add XT60 on the other machine")
    git(second, "push")
    assert control.lib.symbols == []

    control.git_fetch()
    result = control.git_pull()
    assert result.ok, result.transcript()
    assert [s.name for s in control.lib.symbols] == ["XT60"]


def test_audit_blocks_push_returns_only_errors(control, monkeypatch):
    report = check_mod.Report(root=control.root)
    report.add(check_mod.ERROR, "test.broken", "an error")
    report.add(check_mod.WARNING, "test.odd", "a warning")
    report.add(check_mod.INFO, "test.note", "a note")
    monkeypatch.setattr(control, "audit", lambda **k: report)
    errors = control.audit_blocks_push()
    assert [f.code for f in errors] == ["test.broken"]


def test_push_is_refused_when_the_audit_has_errors(control, monkeypatch):
    commit_in(control.root, "a.txt", "local")
    report = check_mod.Report(root=control.root)
    report.add(check_mod.ERROR, "test.broken", "an error")
    monkeypatch.setattr(control, "audit", lambda **k: report)
    result = control.git_push()
    assert not result.ok
    assert "1 error(s)" in result.stderr
    assert control.git_status().ahead == 1


def test_skip_check_overrides_the_audit(control, monkeypatch):
    """
    The override exists because this is one person's library on two machines:
    being unable to park a knowingly-broken state on the remote would be
    worse than the risk of pushing one.
    """
    commit_in(control.root, "a.txt", "local")
    report = check_mod.Report(root=control.root)
    report.add(check_mod.ERROR, "test.broken", "an error")
    monkeypatch.setattr(control, "audit", lambda **k: report)
    assert control.git_push(skip_check=True).ok
    assert control.git_status().ahead == 0


# --------------------------------------------------------------------------
# Tk
# --------------------------------------------------------------------------

tk = pytest.importorskip("tkinter")


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
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def sync(tk_root, control):
    from src.gui.sync_dialog import SyncDialog

    dialog = SyncDialog(tk_root, control)
    # The audit is kicked off in the constructor; settle it so the push row
    # is in its final state rather than "auditing...".
    dialog._audit_errors = []
    dialog._busy = ""
    dialog.refresh()
    yield dialog
    dialog.destroy()


def reason(dialog, action: str) -> str:
    return dialog.reasons[action].cget("text")


def enabled(dialog, action: str) -> bool:
    return str(dialog.buttons[action].cget("state")) == "normal"


# -- the strip -------------------------------------------------------------

@pytest.mark.usefixtures("tk_root")
class TestStrip:
    pytestmark = needs_tk

    def test_the_strip_shows_the_summary_line(self, tk_root, control):
        from src.gui.widgets import GitStrip
        strip = GitStrip(tk_root, on_open=lambda: None)
        strip.update_from(control.git_summary_line(), control.git_status().state)
        assert "main" in strip.text
        assert strip.state == vcs.IN_SYNC

    @pytest.mark.parametrize("state", [
        vcs.IN_SYNC, vcs.AHEAD, vcs.BEHIND, vcs.DIRTY, vcs.DIVERGED,
        vcs.DETACHED, vcs.NO_UPSTREAM, vcs.UNMERGED, vcs.OPERATION_IN_PROGRESS,
    ])
    def test_the_strip_renders_for_every_state(self, tk_root, state):
        from src.gui.widgets import GitStrip
        strip = GitStrip(tk_root, on_open=lambda: None)
        strip.update_from(f"main · {state}", state)
        assert strip.state == state
        assert strip.text

    def test_only_the_states_that_need_attention_are_emphasised(self, tk_root):
        """
        "ahead" and "dirty" are the normal state of a two-machine library
        between sittings. Colouring them would train the eye to ignore the
        colour.
        """
        from src.gui.widgets import GitStrip
        assert vcs.AHEAD not in GitStrip.ALARMING
        assert vcs.DIRTY not in GitStrip.ALARMING
        for state in (vcs.DIVERGED, vcs.UNMERGED, vcs.DETACHED,
                      vcs.OPERATION_IN_PROGRESS):
            assert state in GitStrip.ALARMING

    def test_the_button_opens_the_sync_view(self, tk_root):
        from src.gui.widgets import GitStrip
        opened = []
        strip = GitStrip(tk_root, on_open=lambda: opened.append(1))
        strip.button.invoke()
        assert opened == [1]


@pytest.mark.usefixtures("tk_root")
class TestApp:
    pytestmark = needs_tk

    def test_the_strip_is_absent_when_the_library_is_not_a_repo(self, tk_root, lib_root):
        """Absent, not erroring: using the library without git is normal."""
        from src.gui.app import LibraryManagerApp
        app = LibraryManagerApp(tk_root, lib_root)
        tk_root.update_idletasks()
        assert not app.git_strip.winfo_ismapped()
        assert app.root.title() == app.base_title

    def test_the_strip_appears_and_the_title_reflects_the_state(self, tk_root, git_repo):
        from src.gui.app import LibraryManagerApp
        commit_in(git_repo, "a.txt", "local")
        app = LibraryManagerApp(tk_root, git_repo)
        app.refresh()
        tk_root.update_idletasks()
        assert "main" in app.git_strip.text
        assert "↑1" in app.root.title()

    def test_opening_sync_outside_a_repo_explains_rather_than_opening(
        self, tk_root, lib_root, monkeypatch
    ):
        from src.gui import app as app_mod
        shown = []
        monkeypatch.setattr(app_mod.messagebox, "showinfo",
                            lambda *a, **k: shown.append(a))
        app = app_mod.LibraryManagerApp(tk_root, lib_root)
        app.on_sync()
        assert shown and "not a git repository" in shown[0][0].lower()


@pytest.mark.usefixtures("tk_root")
class TestSyncDialog:
    pytestmark = needs_tk

    def test_it_constructs_and_fills_the_header(self, sync):
        assert "in_sync" in sync._header_vars["state"].get()
        assert "origin/main" in sync._header_vars["upstream"].get()
        assert "Initial commit" in sync._header_vars["head"].get()
        assert sync._header_vars["fetched"].get()

    def test_a_clean_repo_offers_only_fetch(self, sync):
        assert enabled(sync, "fetch")
        for action in ("pull", "commit", "push", "commit_push"):
            assert not enabled(sync, action)
            assert reason(sync, action), f"{action} is disabled with no reason"

    def test_every_disabled_button_states_its_reason(self, sync):
        """A greyed button with no explanation is what this view exists to avoid."""
        for action, button in sync.buttons.items():
            if str(button.cget("state")) != "normal":
                assert reason(sync, action).strip()

    def test_a_dirty_tree_enables_commit_and_explains_the_push(self, tk_root, control):
        # A local commit first, so the push is blocked by the dirty tree
        # rather than by having nothing to send.
        commit_in(control.root, "earlier.txt", "local")
        from src.gui.sync_dialog import SyncDialog
        write(control.root, "symbols/Conn_XT.kicad_symdir/XT60.kicad_sym", "(symbol)\n")
        dialog = SyncDialog(tk_root, control)
        dialog._audit_errors = []
        dialog._busy = ""
        dialog.refresh()
        try:
            assert enabled(dialog, "commit")
            assert dialog._commit_message() == "Add 1 symbol to Conn_XT"
            assert "uncommitted" in reason(dialog, "push")
        finally:
            dialog.destroy()

    def test_a_diverged_branch_refuses_pull_and_push_pointing_at_a_terminal(
        self, tk_root, control, second
    ):
        from src.gui.sync_dialog import SyncDialog
        commit_in(second, "theirs.txt", "theirs")
        git(second, "push")
        commit_in(control.root, "ours.txt", "ours")
        control.git_fetch()
        dialog = SyncDialog(tk_root, control)
        dialog._audit_errors = []
        dialog._busy = ""
        dialog.refresh()
        try:
            assert not enabled(dialog, "pull")
            assert not enabled(dialog, "push")
            assert "terminal" in reason(dialog, "pull")
            assert "diverged" in reason(dialog, "push")
        finally:
            dialog.destroy()

    def test_no_upstream_is_explained(self, tk_root, control):
        from src.gui.sync_dialog import SyncDialog
        git(control.root, "checkout", "-b", "side")
        control.refresh()
        dialog = SyncDialog(tk_root, control)
        dialog._audit_errors = []
        dialog._busy = ""
        dialog.refresh()
        try:
            assert "git push -u" in reason(dialog, "push")
            assert "no upstream" in reason(dialog, "pull")
        finally:
            dialog.destroy()

    def test_the_override_checkbox_gates_the_push(self, tk_root, control):
        """
        Errors do not make a push impossible -- but they do make it a
        decision, and the default is unchecked every time the view opens.
        """
        from src.gui.sync_dialog import SyncDialog
        commit_in(control.root, "a.txt", "local")
        dialog = SyncDialog(tk_root, control)
        dialog._busy = ""
        dialog._audit_errors = [
            check_mod.Finding(check_mod.ERROR, "test.broken", "an error")
        ]
        dialog.refresh()
        try:
            assert dialog.push_anyway.get() is False
            assert not enabled(dialog, "push")
            assert "Push anyway" in reason(dialog, "push")
            assert "1 error(s)" in dialog.push_anyway_check.cget("text")

            dialog.push_anyway.set(True)
            dialog._update_actions()
            assert enabled(dialog, "push")
            assert reason(dialog, "push") == ""
        finally:
            dialog.destroy()

    def test_the_override_is_not_offered_when_the_audit_is_clean(self, sync):
        assert "disabled" in sync.push_anyway_check.state()
        assert sync.push_anyway_check.cget("text") == "Push anyway"

    def test_the_push_row_says_it_is_still_auditing(self, tk_root, control):
        from src.gui.sync_dialog import SyncDialog
        commit_in(control.root, "a.txt", "local")
        dialog = SyncDialog(tk_root, control)
        try:
            dialog._audit_errors = None
            assert "Auditing" in dialog.audit_reason()
        finally:
            dialog.destroy()

    def test_an_edited_message_survives_a_refresh(self, tk_root, control):
        """Losing a typed commit message to a background refresh is unforgivable."""
        from src.gui.sync_dialog import SyncDialog
        write(control.root, "a.txt")
        dialog = SyncDialog(tk_root, control)
        try:
            dialog._set_message("My own words")
            dialog._on_message_key()
            dialog.refresh()
            assert dialog._commit_message() == "My own words"
            dialog._use_suggestion()
            assert dialog._commit_message() != "My own words"
        finally:
            dialog.destroy()

    def test_a_commit_needs_a_message(self, tk_root, control):
        from src.gui.sync_dialog import SyncDialog
        write(control.root, "a.txt")
        dialog = SyncDialog(tk_root, control)
        dialog._audit_errors = []
        dialog._busy = ""
        try:
            dialog._set_message("")
            dialog._on_message_key()
            dialog._update_actions()
            assert not enabled(dialog, "commit")
            assert reason(dialog, "commit") == "A commit needs a message."
        finally:
            dialog.destroy()

    def test_only_one_job_runs_at_a_time(self, tk_root, control):
        """
        Two concurrent git commands contend for the index lock, and the
        loser fails with something no user should have to read.
        """
        from src.gui.sync_dialog import SyncDialog
        dialog = SyncDialog(tk_root, control)
        try:
            dialog._busy = ""
            assert dialog._start("fetch", lambda: "a") is True
            assert dialog._start("pull", lambda: "b") is False
        finally:
            dialog.destroy()

    def test_the_working_tree_tab_lists_the_changes_conflicts_first(
        self, tk_root, control
    ):
        from src.gui.sync_dialog import SyncDialog
        commit_in(control.root, "gone.txt", "doomed")
        (control.root / "gone.txt").unlink()
        # After the commit: commit_in stages everything, so writing this
        # first would have swept it into the commit.
        write(control.root, "later.txt")
        dialog = SyncDialog(tk_root, control)
        try:
            rows = [dialog.tree_list.item(i, "values")
                    for i in dialog.tree_list.get_children("")]
            assert ("deleted", "gone.txt") in rows
            assert ("untracked", "later.txt") in rows
            assert rows[0][0] == "deleted"       # ordered before untracked
        finally:
            dialog.destroy()

    def test_the_tabs_carry_their_counts(self, tk_root, control, second):
        from src.gui.sync_dialog import SyncDialog
        commit_in(second, "theirs.txt", "theirs")
        git(second, "push")
        control.git_fetch()
        dialog = SyncDialog(tk_root, control)
        try:
            assert dialog.tabs.tab(0, "text") == "Incoming (1)"
            assert dialog.tabs.tab(1, "text") == "Outgoing (0)"
        finally:
            dialog.destroy()

    def test_the_log_records_what_git_said(self, tk_root, control):
        from src.gui.sync_dialog import SyncDialog
        dialog = SyncDialog(tk_root, control)
        try:
            dialog._busy = ""
            dialog._finish("fetch", vcs.GitResult(("fetch",), 0, "", "done\n"))
            text = dialog.log.get("1.0", tk.END)
            assert "$ git fetch" in text
        finally:
            dialog.destroy()

    def test_a_running_job_still_explains_every_greyed_button(self, tk_root, control):
        """
        The audit runs as soon as the view opens, so this is the state the
        user actually sees first. A row of dead buttons with blank reasons
        beside them is what this layout exists to prevent.
        """
        from src.gui.sync_dialog import SyncDialog
        dialog = SyncDialog(tk_root, control)
        try:
            dialog._busy = "audit"
            dialog._update_actions()
            for action in dialog.buttons:
                assert not enabled(dialog, action)
                assert reason(dialog, action).strip(), action
            assert "audit" in dialog.busy_label.cget("text")
        finally:
            dialog.destroy()
