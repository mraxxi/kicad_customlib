"""
Phase 2: file dialog backend selection and helper handling.

The native helpers are driven through stub scripts rather than the real
binaries, so these tests are deterministic and never open a window.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from src.gui import filepicker as fp


@pytest.fixture(autouse=True)
def clean_backend_state(monkeypatch):
    """Each test starts with no override, no failures and no dialog open."""
    monkeypatch.delenv(fp.ENV_OVERRIDE, raising=False)
    fp._failed_backends.clear()
    fp._dialog_open = False
    yield
    fp._failed_backends.clear()
    fp._dialog_open = False


def make_stub(directory: Path, name: str, *, stdout: str = "", code: int = 0) -> Path:
    """A fake dialog helper that prints `stdout` and exits with `code`."""
    path = directory / name
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({code})\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def stub_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d), prepend=os.pathsep)
    return d


# --------------------------------------------------------------------------
# Backend selection
# --------------------------------------------------------------------------

def test_the_override_is_honoured(monkeypatch):
    monkeypatch.setenv(fp.ENV_OVERRIDE, "tk")
    assert fp.available_backend() == fp.BACKEND_TK


def test_an_override_naming_a_missing_helper_falls_back_to_tk(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))          # nothing on PATH
    monkeypatch.setenv(fp.ENV_OVERRIDE, "kdialog")
    assert fp.available_backend() == fp.BACKEND_TK


def test_a_nonsense_override_falls_back_to_tk(monkeypatch):
    monkeypatch.setenv(fp.ENV_OVERRIDE, "something-else")
    assert fp.available_backend() == fp.BACKEND_TK


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_zenity_is_preferred_over_kdialog(stub_dir):
    """
    Not what the desktop would suggest, and measured rather than assumed: on
    the development machine kdialog took a median of 18.5s to show its window
    against zenity's 0.4s. See the module docstring.
    """
    make_stub(stub_dir, "kdialog")
    make_stub(stub_dir, "zenity")
    assert fp.available_backend() == fp.BACKEND_ZENITY


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_kdialog_is_used_when_zenity_is_absent(stub_dir, monkeypatch):
    # An exclusive PATH, not a prepended one: the real zenity is installed on
    # the development machine and would otherwise still be found.
    monkeypatch.setenv("PATH", str(stub_dir))
    make_stub(stub_dir, "kdialog")
    assert fp.available_backend() == fp.BACKEND_KDIALOG


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_the_plasma_dialog_can_still_be_insisted_on(stub_dir, monkeypatch):
    make_stub(stub_dir, "kdialog")
    make_stub(stub_dir, "zenity")
    monkeypatch.setenv(fp.ENV_OVERRIDE, "kdialog")
    assert fp.available_backend() == fp.BACKEND_KDIALOG


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_tk_is_used_when_no_helper_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert fp.available_backend() == fp.BACKEND_TK


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_a_failed_helper_is_not_retried(stub_dir):
    """
    A broken helper would otherwise add a pointless delay to every dialog for
    the rest of the session.
    """
    make_stub(stub_dir, "kdialog")
    make_stub(stub_dir, "zenity")
    assert fp.available_backend() == fp.BACKEND_ZENITY
    fp._failed_backends.add(fp.BACKEND_ZENITY)
    assert fp.available_backend() == fp.BACKEND_KDIALOG


def test_windows_and_macos_always_use_tk(monkeypatch, stub_dir):
    """Tk's dialog *is* the native one there, so shelling out is a downgrade."""
    make_stub(stub_dir, "kdialog")
    monkeypatch.setattr(sys, "platform", "win32")
    assert fp.available_backend() == fp.BACKEND_TK
    monkeypatch.setattr(sys, "platform", "darwin")
    assert fp.available_backend() == fp.BACKEND_TK


# --------------------------------------------------------------------------
# Helper invocation
# --------------------------------------------------------------------------

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_open_files_parses_newline_separated_output(stub_dir, tmp_path):
    make_stub(stub_dir, "zenity", stdout=f"{tmp_path}/a.kicad_sym\n{tmp_path}/b.kicad_mod\n")
    paths = fp.open_files(None, initialdir=tmp_path)
    assert [p.name for p in paths] == ["a.kicad_sym", "b.kicad_mod"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_a_cancelled_dialog_returns_nothing_and_is_not_an_error(stub_dir, tmp_path):
    """Both helpers use exit 1 for "the user cancelled", a normal outcome."""
    make_stub(stub_dir, "zenity", code=1)
    assert fp.open_files(None, initialdir=tmp_path) == []
    assert fp._failed_backends == set()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_a_broken_helper_falls_back_to_tk_and_is_remembered(
    stub_dir, tmp_path, monkeypatch
):
    make_stub(stub_dir, "zenity", code=2)       # neither success nor cancel
    called = []
    monkeypatch.setattr(fp, "_tk_open_files",
                        lambda *a, **k: called.append(1) or [Path("fallback")])
    result = fp.open_files(None, initialdir=tmp_path)
    assert called == [1]
    assert result == [Path("fallback")]
    assert fp.BACKEND_ZENITY in fp._failed_backends


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_open_directory_returns_a_single_path(stub_dir, tmp_path):
    make_stub(stub_dir, "zenity", stdout=f"{tmp_path}\n")
    assert fp.open_directory(None, initialdir=tmp_path) == tmp_path


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_open_directory_cancelled_returns_none(stub_dir, tmp_path):
    make_stub(stub_dir, "zenity", code=1)
    assert fp.open_directory(None, initialdir=tmp_path) is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_blank_lines_in_helper_output_are_ignored(stub_dir, tmp_path):
    make_stub(stub_dir, "zenity", stdout=f"\n{tmp_path}/a.zip\n\n")
    assert [p.name for p in fp.open_files(None, initialdir=tmp_path)] == ["a.zip"]


# --------------------------------------------------------------------------
# Filter formatting
# --------------------------------------------------------------------------

def test_kdialog_filters_use_patterns_pipe_label():
    out = fp._kdialog_filters((("KiCad", "*.kicad_sym"), ("All", "*")))
    assert out == "*.kicad_sym|KiCad\n*|All"


def test_zenity_filters_use_label_pipe_patterns():
    assert fp._zenity_filter_args((("ZIP archives", "*.zip"),)) == [
        "--file-filter=ZIP archives | *.zip"
    ]


def test_tk_filetypes_spell_any_file_the_windows_way():
    """Tk on Windows wants "*.*" rather than a bare "*"."""
    assert fp._tk_filetypes((("All files", "*"),)) == [("All files", "*.*")]


def test_the_shipped_filter_sets_cover_what_ingest_accepts():
    kicad = dict(fp.KICAD_FILTERS)
    patterns = " ".join(kicad.keys()) + " " + " ".join(kicad.values())
    for extension in (".kicad_sym", ".kicad_mod", ".step", ".stp", ".wrl"):
        assert f"*{extension}" in patterns
    assert any("*.zip" in p for _l, p in fp.ARCHIVE_FILTERS)


# --------------------------------------------------------------------------
# Not freezing the application
#
# These pin the bug that made the GUI feel broken: "nothing happens when I
# click Add folder" (it did happen -- a minute or two later), the window not
# resizing, and a black rectangle where the uncovered area should have been.
# All three were one cause: the helper loop called Tk's update(), which
# dispatches input events and so re-entered the callback that opened the
# dialog.
# --------------------------------------------------------------------------

def make_slow_stub(directory: Path, name: str, seconds: float = 0.4) -> Path:
    path = directory / name
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        f"time.sleep({seconds})\n"
        "sys.stdout.write('/tmp/chosen.kicad_sym\\n')\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_nothing_in_the_async_path_waits_on_the_helper():
    """
    The freeze, pinned at the source.

    The first version waited in a loop calling Tk's update(), which
    dispatches input events and so re-entered the callback that opened the
    dialog. Replacing update() with update_idletasks() removed the
    re-entrancy but not the freeze: it flushes pending redraws and nothing
    else, so incoming expose and configure events were still ignored --
    measured, zero redraw cycles and a resize dropped for as long as the
    dialog was open. The only fix is to not wait at all.
    """
    import ast
    import inspect

    # The docstring explains the mistake, so it names the very calls being
    # banned; only the code is checked.
    tree = ast.parse(inspect.getsource(fp._run_helper_async).strip())
    func = tree.body[0]
    if (func.body and isinstance(func.body[0], ast.Expr)
            and isinstance(func.body[0].value, ast.Constant)):
        func.body = func.body[1:]
    code = ast.unparse(func)

    assert "after(" in code, "must be driven from Tk's own event loop"
    for blocking in (".update()", "update_idletasks", "reader.join", "time.sleep"):
        assert blocking not in code, f"{blocking} blocks the event loop"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_the_event_loop_keeps_running_while_a_dialog_is_open(stub_dir, tmp_path):
    """
    End to end, with a real Tk root: timers must keep firing and a resize
    must be honoured while the helper is up. This is the black rectangle and
    the dead window, as a test.
    """
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:  # noqa: BLE001
        pytest.skip("no display available for Tk")
    root.withdraw()
    root.geometry("400x300")

    make_slow_stub(stub_dir, "zenity", seconds=1.0)
    ticks, result = [], []

    try:
        def tick() -> None:
            ticks.append(1)
            if len(ticks) < 500:
                root.after(10, tick)
        root.after(10, tick)

        fp.open_files(root, initialdir=tmp_path, on_done=result.append)
        # open_files returned straight away, before the helper finished.
        assert result == []

        # The test plays the part of mainloop(), bounded so it cannot hang.
        deadline = time.monotonic() + 20
        while not result and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)

        assert result, "the callback never fired"
        assert [p.name for p in result[0]] == ["chosen.kicad_sym"]
        assert len(ticks) > 20, f"only {len(ticks)} timer ticks while open"
        assert fp._dialog_open is False
    finally:
        root.destroy()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_a_resize_is_honoured_while_a_dialog_is_open(stub_dir, tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:  # noqa: BLE001
        pytest.skip("no display available for Tk")
    root.withdraw()
    root.geometry("400x300")
    root.update()

    make_slow_stub(stub_dir, "zenity", seconds=0.8)
    result, seen = [], []
    try:
        fp.open_files(root, initialdir=tmp_path, on_done=result.append)
        root.after(100, lambda: root.geometry("640x480"))
        deadline = time.monotonic() + 20
        while not result and time.monotonic() < deadline:
            root.update()
            seen.append((root.winfo_width(), root.winfo_height()))
            time.sleep(0.01)
        assert (640, 480) in seen, f"resize never applied; saw {sorted(set(seen))}"
    finally:
        root.destroy()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_a_second_click_while_a_dialog_is_open_is_ignored(stub_dir, tmp_path):
    """Two file dialogs at once has no meaning, and was what re-entrancy produced."""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:  # noqa: BLE001
        pytest.skip("no display available for Tk")
    root.withdraw()
    make_slow_stub(stub_dir, "zenity", seconds=0.6)
    first, second = [], []
    try:
        fp.open_files(root, initialdir=tmp_path, on_done=first.append)
        fp.open_files(root, initialdir=tmp_path, on_done=second.append)
        deadline = time.monotonic() + 20
        while not first and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert first, "the first dialog never completed"
        assert second == [], "a second dialog was opened"
    finally:
        root.destroy()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_an_async_helper_failure_falls_back_to_tk(stub_dir, tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:  # noqa: BLE001
        pytest.skip("no display available for Tk")
    root.withdraw()
    make_stub(stub_dir, "zenity", code=2)        # neither success nor cancel
    monkeypatch.setattr(fp, "_tk_open_files",
                        lambda *a, **k: [Path("/tmp/fallback.kicad_sym")])
    result = []
    try:
        fp.open_files(root, initialdir=tmp_path, on_done=result.append)
        deadline = time.monotonic() + 20
        while not result and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert [p.name for p in result[0]] == ["fallback.kicad_sym"]
        assert fp.BACKEND_ZENITY in fp._failed_backends
        assert fp._dialog_open is False
    finally:
        root.destroy()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_the_directory_chooser_is_asynchronous_too(stub_dir, tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:  # noqa: BLE001
        pytest.skip("no display available for Tk")
    root.withdraw()
    path = stub_dir / "zenity"
    path.write_text("#!/usr/bin/env python3\nimport sys,time\ntime.sleep(0.3)\n"
                    f"sys.stdout.write({str(tmp_path)!r} + '\\n')\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    result = []
    try:
        assert fp.open_directory(root, initialdir=tmp_path,
                                 on_done=result.append) is None
        deadline = time.monotonic() + 20
        while not result and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert result == [tmp_path]
    finally:
        root.destroy()
