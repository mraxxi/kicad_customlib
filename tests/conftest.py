"""
Shared pytest fixtures.

`lib_root` gives every test its own throwaway library root with the same
three-way layout as the real repo, so nothing here can touch the actual
symbols/, footprints/ or 3dmodels/ trees.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# scripts/ is the import root: production code does `from src.core...`
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tests import kicad_fixtures as kf  # noqa: E402


def filesystem_is_case_insensitive(directory: Path) -> bool:
    """
    Whether `directory` lives on a filesystem that folds case.

    Windows and macOS do by default, which means a test cannot create two
    files differing only by case in order to check that we *detect* that
    situation -- the second write just replaces the first. Probed rather than
    inferred from sys.platform, because either platform can be configured
    the other way.
    """
    probe = directory / "CaseProbe.tmp"
    probe.write_text("x", encoding="utf-8")
    try:
        return (directory / "caseprobe.tmp").exists()
    finally:
        probe.unlink()


@pytest.fixture
def fixtures():
    """The builder module, for tests that want to construct bespoke files."""
    return kf


@pytest.fixture
def lib_root(tmp_path: Path) -> Path:
    """An empty but correctly shaped library root."""
    root = tmp_path / "KICAD_CUSTOM_LIB"
    for sub in ("symbols", "footprints", "3dmodels"):
        (root / sub).mkdir(parents=True)
    return root


@pytest.fixture
def populated_lib(lib_root: Path) -> Path:
    """
    A small but complete library:

      Amp_Test       TPA3255DDV  -> Amp_Test:SOP63P810X120-44N -> TPA3255DDV.step
                     TPA3251DDV  -> (extends TPA3255DDV, same symdir, sibling file)
      Conn_Test      KF2EDG      -> Conn_Test:KF2EDG-5.08_4P   -> (no model)

    Covers: a resolved symbol->footprint->model chain, a derived symbol in a
    sibling file, a footprint with no model, and two categories.
    """
    amp_sym = lib_root / "symbols" / "Amp_Test.kicad_symdir"
    amp_fp = lib_root / "footprints" / "Amp_Test.pretty"
    amp_3d = lib_root / "3dmodels" / "Amp_Test.3dshapes"
    conn_sym = lib_root / "symbols" / "Conn_Test.kicad_symdir"
    conn_fp = lib_root / "footprints" / "Conn_Test.pretty"

    kf.write_symbol(
        amp_sym / "TPA3255DDV.kicad_sym",
        footprint="Amp_Test:SOP63P810X120-44N",
    )
    kf.write_symbol(
        amp_sym / "TPA3251DDV.kicad_sym",
        footprint="Amp_Test:SOP63P810X120-44N",
        extends="TPA3255DDV",
    )
    kf.write_footprint(
        amp_fp / "SOP63P810X120-44N.kicad_mod",
        model="${KICAD_CUSTOM_LIB}/3dmodels/Amp_Test.3dshapes/TPA3255DDV.step",
    )
    kf.write_step(amp_3d / "TPA3255DDV.step")

    kf.write_symbol(
        conn_sym / "KF2EDG.kicad_sym",
        footprint="Conn_Test:KF2EDG-5.08_4P",
    )
    kf.write_footprint(conn_fp / "KF2EDG-5.08_4P.kicad_mod")

    return lib_root


# --------------------------------------------------------------------------
# Git fixtures
#
# A throwaway repository plus a `git init --bare` stand-in for origin, and a
# second clone of it. The second clone is what makes "behind", "diverged" and
# a genuinely conflicted merge reachable with no network at all.
# --------------------------------------------------------------------------

has_git = pytest.mark.skipif(shutil.which("git") is None,
                             reason="git is not installed")


def git(root: Path, *args: str) -> str:
    """Run git in `root` for test setup, failing loudly."""
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        # The user's own git config must not reach these tests: a global
        # commit.gpgsign or a merge driver would make them fail for reasons
        # that have nothing to do with the code.
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "LC_ALL": "C",
    })
    proc = subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"git {' '.join(args)}\n{proc.stdout}{proc.stderr}"
    return proc.stdout


def write_file(root: Path, rel: str, content: str = "x\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def commit_in(root: Path, rel: str, message: str) -> None:
    write_file(root, rel, f"{message}\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", message)


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    """A local stand-in for origin."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   capture_output=True, check=True)
    return remote


@pytest.fixture
def git_repo(tmp_path: Path, bare: Path) -> Path:
    """A clone with one commit, tracking `main`, clean and in sync."""
    root = tmp_path / "work"
    for sub in ("symbols", "footprints", "3dmodels"):
        (root / sub).mkdir(parents=True)
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "remote", "add", "origin", str(bare))
    write_file(root, "README.md", "library\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", "Initial commit")
    git(root, "push", "-u", "origin", "main")
    return root


@pytest.fixture
def second(tmp_path: Path, bare: Path, git_repo: Path) -> Path:
    """A second clone of the same remote -- the other machine."""
    root = tmp_path / "other"
    subprocess.run(["git", "clone", str(bare), str(root)],
                   capture_output=True, check=True)
    git(root, "config", "user.name", "Other")
    git(root, "config", "user.email", "other@example.com")
    git(root, "config", "commit.gpgsign", "false")
    return root
