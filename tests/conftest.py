"""
Shared pytest fixtures.

`lib_root` gives every test its own throwaway library root with the same
three-way layout as the real repo, so nothing here can touch the actual
symbols/, footprints/ or 3dmodels/ trees.
"""

from __future__ import annotations

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
