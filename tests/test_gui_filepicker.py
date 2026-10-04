"""
Phase 2: file dialog backend selection and helper handling.

The native helpers are driven through stub scripts rather than the real
binaries, so these tests are deterministic and never open a window.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from src.gui import filepicker as fp


@pytest.fixture(autouse=True)
def clean_backend_state(monkeypatch):
    """Each test starts with no override and no remembered failures."""
    monkeypatch.delenv(fp.ENV_OVERRIDE, raising=False)
    fp._failed_backends.clear()
    yield
    fp._failed_backends.clear()


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
def test_kdialog_is_preferred_over_zenity(stub_dir):
    """On KDE the Plasma dialog is the one worth having."""
    make_stub(stub_dir, "kdialog")
    make_stub(stub_dir, "zenity")
    assert fp.available_backend() == fp.BACKEND_KDIALOG


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_zenity_is_used_when_kdialog_is_absent(stub_dir):
    make_stub(stub_dir, "zenity")
    assert fp.available_backend() == fp.BACKEND_ZENITY


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_tk_is_used_when_no_helper_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert fp.available_backend() == fp.BACKEND_TK


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_a_failed_helper_is_not_retried(stub_dir):
    """
    A broken kdialog would otherwise add a pointless delay to every dialog
    for the rest of the session.
    """
    make_stub(stub_dir, "kdialog")
    make_stub(stub_dir, "zenity")
    assert fp.available_backend() == fp.BACKEND_KDIALOG
    fp._failed_backends.add(fp.BACKEND_KDIALOG)
    assert fp.available_backend() == fp.BACKEND_ZENITY


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
    make_stub(stub_dir, "kdialog", stdout=f"{tmp_path}/a.kicad_sym\n{tmp_path}/b.kicad_mod\n")
    paths = fp.open_files(None, initialdir=tmp_path)
    assert [p.name for p in paths] == ["a.kicad_sym", "b.kicad_mod"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_a_cancelled_dialog_returns_nothing_and_is_not_an_error(stub_dir, tmp_path):
    """Both helpers use exit 1 for "the user cancelled", a normal outcome."""
    make_stub(stub_dir, "kdialog", code=1)
    assert fp.open_files(None, initialdir=tmp_path) == []
    assert fp._failed_backends == set()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only path")
def test_a_broken_helper_falls_back_to_tk_and_is_remembered(
    stub_dir, tmp_path, monkeypatch
):
    make_stub(stub_dir, "kdialog", code=2)      # neither success nor cancel
    called = []
    monkeypatch.setattr(fp, "_tk_open_files",
                        lambda *a, **k: called.append(1) or [Path("fallback")])
    result = fp.open_files(None, initialdir=tmp_path)
    assert called == [1]
    assert result == [Path("fallback")]
    assert fp.BACKEND_KDIALOG in fp._failed_backends


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
