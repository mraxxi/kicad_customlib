"""
Phase 0: pin down how KiCad 10 actually stores symbol libraries.

The tests that read /usr/share/kicad are the *source* of the invariants
recorded in AGENTS.md; they skip when KiCad is not installed (e.g. in CI).
The tests that read our own fixtures assert the fixtures keep encoding those
same invariants, so they run everywhere.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import kicad_fixtures as kf

OFFICIAL_SYMBOLS = Path(
    os.environ.get("KICAD10_SYMBOL_DIR", "/usr/share/kicad/symbols")
)
HAS_OFFICIAL = OFFICIAL_SYMBOLS.is_dir()
KICAD_CLI = shutil.which("kicad-cli")

needs_official = pytest.mark.skipif(
    not HAS_OFFICIAL, reason=f"official KiCad symbol libraries not found at {OFFICIAL_SYMBOLS}"
)
needs_cli = pytest.mark.skipif(KICAD_CLI is None, reason="kicad-cli not on PATH")

# A top-level symbol is indented exactly one tab; unit sub-symbols are deeper.
TOP_LEVEL_SYMBOL = re.compile(r'^\t\(symbol "([^"]+)"', re.M)
EXTENDS = re.compile(r'^\t\t\(extends "([^"]+)"', re.M)


def _sample_official(limit: int = 300) -> list[Path]:
    """A deterministic spread of official symbol files."""
    files = sorted(OFFICIAL_SYMBOLS.glob("*.kicad_symdir/*.kicad_sym"))
    if not files:
        return []
    step = max(1, len(files) // limit)
    return files[::step][:limit]


# --------------------------------------------------------------------------
# Invariant 1: libraries are directories, one symbol per file
# --------------------------------------------------------------------------

@needs_official
def test_official_symbol_libraries_are_symdir_directories():
    symdirs = list(OFFICIAL_SYMBOLS.glob("*.kicad_symdir"))
    loose = list(OFFICIAL_SYMBOLS.glob("*.kicad_sym"))
    assert symdirs, "expected .kicad_symdir directories"
    assert all(d.is_dir() for d in symdirs)
    assert not loose, f"unexpected loose .kicad_sym at the symbols root: {loose[:3]}"


@needs_official
def test_official_files_hold_exactly_one_top_level_symbol():
    offenders = []
    for f in _sample_official():
        names = TOP_LEVEL_SYMBOL.findall(f.read_text(encoding="utf-8"))
        if len(names) != 1:
            offenders.append((f.name, names))
    assert offenders == [], f"files with != 1 top-level symbol: {offenders[:5]}"


@needs_official
def test_official_internal_symbol_name_matches_filename_stem_case_insensitively():
    """
    Across all 22784 official files the internal name equals the filename stem,
    with exactly one exception: Interface_USB/tusb564.kicad_sym declares
    (symbol "TUSB564"). So the rule is real but must be enforced
    case-insensitively -- an exact-match assumption would reject a file KiCad
    itself ships. Tools should warn on a case-only difference, not fail.
    """
    hard = []
    case_only = []
    for f in _sample_official():
        names = TOP_LEVEL_SYMBOL.findall(f.read_text(encoding="utf-8"))
        if len(names) != 1 or names[0] == f.stem:
            continue
        (case_only if names[0].lower() == f.stem.lower() else hard).append((f.stem, names[0]))
    assert hard == [], f"internal name differs by more than case: {hard[:5]}"


@needs_official
def test_official_filename_charset_is_a_subset_of_our_allowed_names():
    """
    Official filenames use only alphanumerics plus '+', '-', '.', '_'. That is
    exactly the character set naming.py allows, so our validation rules will
    never reject a name KiCad's own libraries use.
    """
    allowed = set("+-._")
    bad = set()
    for f in _sample_official(500):
        bad |= {c for c in f.stem if not c.isalnum() and c not in allowed}
    assert bad == set(), f"unexpected characters in official filenames: {sorted(bad)}"


# --------------------------------------------------------------------------
# Invariant 2: `extends` crosses files, never stays inside one
# --------------------------------------------------------------------------

@needs_official
def test_official_extends_always_targets_a_sibling_file_in_the_same_symdir():
    """
    This is the invariant the original plan had backwards. A derived symbol
    lives in its own file and names its parent, which is a *different file*
    in the same .kicad_symdir. Renaming a symbol therefore has to rewrite
    siblings, not just the file being renamed.
    """
    checked = 0
    outside = []
    inside_same_file = []
    for f in _sample_official():
        text = f.read_text(encoding="utf-8")
        parents = EXTENDS.findall(text)
        if not parents:
            continue
        checked += 1
        for parent in parents:
            if not (f.parent / f"{parent}.kicad_sym").exists():
                outside.append((f.name, parent))
            if f'\t(symbol "{parent}"' in text:
                inside_same_file.append((f.name, parent))
    assert checked > 0, "sample contained no derived symbols; widen the sample"
    assert outside == [], f"extends target missing from its own symdir: {outside[:5]}"
    assert inside_same_file == [], (
        f"extends target defined in the same file: {inside_same_file[:5]}"
    )


# --------------------------------------------------------------------------
# Invariant 3: unit sub-symbol naming
# --------------------------------------------------------------------------

@needs_official
def test_official_unit_subsymbols_are_prefixed_with_the_parent_name():
    bad = []
    for f in _sample_official(120):
        text = f.read_text(encoding="utf-8")
        top = TOP_LEVEL_SYMBOL.findall(text)
        if len(top) != 1:
            continue
        parent = top[0]
        for unit in re.findall(r'^\t\t\(symbol "([^"]+)"', text, re.M):
            if not unit.startswith(f"{parent}_"):
                bad.append((f.name, parent, unit))
    assert bad == [], f"unit sub-symbols not prefixed by parent name: {bad[:5]}"


def test_fixture_unit_subsymbols_follow_the_naming_scheme(tmp_path):
    f = kf.write_symbol(tmp_path / "ACME1.kicad_sym", units=("0_1", "1_1"))
    text = f.read_text(encoding="utf-8")
    assert TOP_LEVEL_SYMBOL.findall(text) == ["ACME1"]
    assert re.findall(r'^\t\t\(symbol "([^"]+)"', text, re.M) == ["ACME1_0_1", "ACME1_1_1"]


def test_fixture_derived_symbol_is_a_sibling_file(tmp_path):
    d = tmp_path / "Amp.kicad_symdir"
    kf.write_symbol(d / "BASE.kicad_sym")
    derived = kf.write_symbol(d / "DERIVED.kicad_sym", extends="BASE")
    text = derived.read_text(encoding="utf-8")
    assert EXTENDS.findall(text) == ["BASE"]
    assert (d / "BASE.kicad_sym").exists()
    # the parent is NOT duplicated into the derived file
    assert TOP_LEVEL_SYMBOL.findall(text) == ["DERIVED"]


# --------------------------------------------------------------------------
# Invariant 4: our fixtures are files KiCad itself accepts
# --------------------------------------------------------------------------

@needs_cli
def test_kicad_cli_accepts_our_symbol_fixtures(tmp_path):
    """A symdir with a base symbol and a derived sibling must plot cleanly."""
    d = tmp_path / "Amp.kicad_symdir"
    kf.write_symbol(d / "TPA3255DDV.kicad_sym", footprint="Amp:FP1")
    kf.write_symbol(d / "TPA3251DDV.kicad_sym", footprint="Amp:FP1", extends="TPA3255DDV")
    out = tmp_path / "svg"
    out.mkdir()
    r = subprocess.run(
        [KICAD_CLI, "sym", "export", "svg", "-o", str(out), str(d)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    # Two symbols in, two SVGs out -- including the derived one, which proves
    # kicad-cli resolved `extends` across sibling files.
    assert len(list(out.glob("*.svg"))) == 2, sorted(p.name for p in out.glob("*"))


@needs_cli
def test_kicad_cli_accepts_our_footprint_fixtures(tmp_path):
    d = tmp_path / "Amp.pretty"
    kf.write_footprint(d / "FP1.kicad_mod", model="${KICAD_CUSTOM_LIB}/3dmodels/Amp.3dshapes/m.step")
    kf.write_footprint(d / "FP2.kicad_mod")  # no model block
    out = tmp_path / "svg"
    out.mkdir()
    r = subprocess.run(
        [KICAD_CLI, "fp", "export", "svg", "-o", str(out), str(d)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert len(list(out.glob("*.svg"))) == 2


# --------------------------------------------------------------------------
# Invariant 5: how kicad-cli reports breakage (drives check.py)
# --------------------------------------------------------------------------

@needs_cli
def test_fp_export_svg_exits_nonzero_on_a_corrupt_footprint(tmp_path):
    """Footprints: the exit code alone is a trustworthy signal."""
    d = tmp_path / "Bad.pretty"
    d.mkdir()
    (d / "X.kicad_mod").write_text('(footprint "X"\n\t(version 20260206)\n\t(pad "1" smd rect\n')
    out = tmp_path / "svg"
    out.mkdir()
    r = subprocess.run(
        [KICAD_CLI, "fp", "export", "svg", "-o", str(out), str(d)],
        capture_output=True, text=True,
    )
    assert r.returncode != 0


@needs_cli
def test_sym_export_svg_exits_zero_even_on_a_corrupt_symbol(tmp_path):
    """
    Symbols: `sym export svg` is NOT a usable validator -- it returns 0 and
    silently writes nothing. check.py must compare the SVG count against the
    expected symbol count, or use `sym upgrade` on a copy instead.
    """
    d = tmp_path / "Bad.kicad_symdir"
    d.mkdir()
    (d / "X.kicad_sym").write_text('(kicad_symbol_lib\n\t(version 20251024)\n\t(symbol "X"\n')
    out = tmp_path / "svg"
    out.mkdir()
    r = subprocess.run(
        [KICAD_CLI, "sym", "export", "svg", "-o", str(out), str(d)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, "if this starts failing, exit codes became usable"
    assert list(out.glob("*.svg")) == [], "corrupt library should produce no SVG"


@needs_cli
def test_sym_upgrade_exits_nonzero_on_a_corrupt_symbol(tmp_path):
    """
    `sym upgrade` does detect corruption, but it rewrites files in place, so
    check.py must only ever run it against a throwaway copy.
    """
    d = tmp_path / "Bad.kicad_symdir"
    d.mkdir()
    (d / "X.kicad_sym").write_text('(kicad_symbol_lib\n\t(version 20251024)\n\t(symbol "X"\n')
    r = subprocess.run([KICAD_CLI, "sym", "upgrade", str(d)], capture_output=True, text=True)
    assert r.returncode != 0
