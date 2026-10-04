"""Phase 1: check.py -- a real audit that can fail a build."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.core import check
from src.core import library as lb
from src.core import provenance as pv
from src.core import table_gen as tg
from tests import kicad_fixtures as kf

HAS_CLI = shutil.which("kicad-cli") is not None


def codes(report, severity=None):
    return sorted(f.code for f in report.findings
                  if severity is None or f.severity == severity)


def _healthy(lib_root: Path) -> Path:
    """A library with nothing wrong with it."""
    kf.write_symbol(
        lib_root / "symbols" / "Amp.kicad_symdir" / "TPA3255DDV.kicad_sym",
        footprint="Amp:SOP63",
    )
    kf.write_footprint(
        lib_root / "footprints" / "Amp.pretty" / "SOP63.kicad_mod",
        model="${KICAD_CUSTOM_LIB}/3dmodels/Amp.3dshapes/SOP63.step",
    )
    kf.write_step(lib_root / "3dmodels" / "Amp.3dshapes" / "SOP63.step")
    tg.generate_tables(lib_root)
    return lib_root


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------

def test_a_healthy_library_reports_no_errors(lib_root):
    report = check.run(_healthy(lib_root))
    assert report.errors == [], report.summary()
    assert report.exit_code == 0


def test_an_empty_library_is_clean_once_the_tables_are_generated(lib_root):
    tg.generate_tables(lib_root)
    report = check.run(lib_root)
    assert report.exit_code == 0


def test_check_never_modifies_anything(lib_root):
    _healthy(lib_root)
    before = {p: p.read_bytes() for p in lib_root.rglob("*") if p.is_file()}
    check.run(lib_root)
    after = {p: p.read_bytes() for p in lib_root.rglob("*") if p.is_file()}
    assert before == after


# --------------------------------------------------------------------------
# Exit code
# --------------------------------------------------------------------------

def test_exit_code_is_nonzero_when_an_error_exists(lib_root):
    """The old `check` always exited 0, so CI could never gate on it."""
    _healthy(lib_root)
    (lib_root / "3dmodels" / "Amp.3dshapes" / "SOP63.step").unlink()
    report = check.run(lib_root)
    assert report.has_errors
    assert report.exit_code == 1


def test_warnings_alone_do_not_fail_the_check(lib_root):
    _healthy(lib_root)
    (lib_root / "symbols" / "Empty_Cat.kicad_symdir").mkdir()
    tg.generate_tables(lib_root)
    report = check.run(lib_root)
    assert "category-empty" in codes(report, check.WARNING)
    assert report.exit_code == 0


# --------------------------------------------------------------------------
# The real repo's failure modes
# --------------------------------------------------------------------------

def test_stale_tables_are_an_error(lib_root):
    """Exactly this repo's state: tables listing categories the disk lacks."""
    _healthy(lib_root)
    kf.write_symbol(lib_root / "symbols" / "New.kicad_symdir" / "S.kicad_sym")
    report = check.run(lib_root)
    assert "table-stale" in codes(report, check.ERROR)
    assert any("generate" in f.remedy for f in report.errors)


def test_empty_categories_are_reported_with_the_git_consequence(lib_root):
    for cat in ("3255", "TI-TPAxxx_AUDIO-AMP"):
        (lib_root / "symbols" / f"{cat}.kicad_symdir").mkdir()
        (lib_root / "footprints" / f"{cat}.pretty").mkdir()
        (lib_root / "3dmodels" / f"{cat}.3dshapes").mkdir()
    tg.generate_tables(lib_root)
    report = check.run(lib_root)
    empties = [f for f in report.findings if f.code == "category-empty"]
    assert len(empties) == 2
    assert any("git does not track empty directories" in f.message for f in empties)


def test_a_multi_symbol_file_is_flagged(lib_root):
    kf.write_multi_symbol(
        lib_root / "symbols" / "Cache.kicad_symdir" / "TPA3255DDV.kicad_sym",
        ["TPA3255DDV", "CONN-5MM-4P"],
    )
    tg.generate_tables(lib_root)
    report = check.run(lib_root)
    assert "symbol-file-multi" in codes(report, check.WARNING)


# --------------------------------------------------------------------------
# Broken references
# --------------------------------------------------------------------------

def test_a_dangling_footprint_reference_is_an_error(lib_root):
    kf.write_symbol(
        lib_root / "symbols" / "Amp.kicad_symdir" / "S.kicad_sym", footprint="Amp:GONE"
    )
    tg.generate_tables(lib_root)
    report = check.run(lib_root)
    assert "footprint-ref-broken" in codes(report, check.ERROR)


def test_a_missing_model_file_is_an_error(lib_root):
    kf.write_footprint(
        lib_root / "footprints" / "Amp.pretty" / "FP.kicad_mod",
        model="${KICAD_CUSTOM_LIB}/3dmodels/Amp.3dshapes/gone.step",
    )
    tg.generate_tables(lib_root)
    assert "model-file-missing" in codes(check.run(lib_root), check.ERROR)


def test_an_absolute_model_path_is_an_error(lib_root):
    kf.write_footprint(
        lib_root / "footprints" / "Amp.pretty" / "FP.kicad_mod",
        model="/home/archvan/models/M.step",
    )
    tg.generate_tables(lib_root)
    report = check.run(lib_root)
    assert "model-path-not-portable" in codes(report, check.ERROR)
    assert any("another machine" in f.message for f in report.errors)


def test_a_kiprjmod_model_path_in_the_library_is_an_error(lib_root):
    """${KIPRJMOD} only means anything inside a project, not in the library."""
    kf.write_footprint(
        lib_root / "footprints" / "Amp.pretty" / "FP.kicad_mod",
        model="${KIPRJMOD}/project_libs/3dmodels/Amp.3dshapes/M.step",
    )
    tg.generate_tables(lib_root)
    assert "model-path-not-portable" in codes(check.run(lib_root), check.ERROR)


def test_a_derived_symbol_with_no_parent_is_an_error(lib_root):
    kf.write_symbol(
        lib_root / "symbols" / "Amp.kicad_symdir" / "D.kicad_sym", extends="MISSING"
    )
    tg.generate_tables(lib_root)
    report = check.run(lib_root)
    assert "extends-parent-missing" in codes(report, check.ERROR)
    assert any("same symdir" in f.message for f in report.errors)


def test_a_footprint_whose_internal_name_disagrees_is_a_warning(lib_root):
    kf.write_footprint(
        lib_root / "footprints" / "Amp.pretty" / "FILENAME.kicad_mod", name="INTERNAL"
    )
    tg.generate_tables(lib_root)
    assert "footprint-name-mismatch" in codes(check.run(lib_root), check.WARNING)


def test_the_official_case_only_mismatch_is_only_a_note(lib_root):
    """KiCad ships tusb564.kicad_sym declaring (symbol "TUSB564")."""
    kf.write_symbol(
        lib_root / "symbols" / "Iface.kicad_symdir" / "tusb564.kicad_sym", name="TUSB564"
    )
    tg.generate_tables(lib_root)
    report = check.run(lib_root)
    assert "symbol-name-case" in codes(report, check.INFO)
    assert "symbol-name-mismatch" not in codes(report)
    assert report.exit_code == 0


# --------------------------------------------------------------------------
# Filesystem hazards
# --------------------------------------------------------------------------

def test_case_colliding_categories_are_an_error(lib_root):
    kf.write_symbol(lib_root / "symbols" / "Amp.kicad_symdir" / "S.kicad_sym")
    kf.write_footprint(lib_root / "footprints" / "amp.pretty" / "F.kicad_mod")
    tg.generate_tables(lib_root)
    report = check.run(lib_root)
    assert "category-case-collision" in codes(report, check.ERROR)


def test_case_colliding_symbols_in_one_category_are_an_error(lib_root):
    d = lib_root / "symbols" / "Amp.kicad_symdir"
    kf.write_symbol(d / "TPA3255.kicad_sym")
    kf.write_symbol(d / "tpa3255.kicad_sym")
    tg.generate_tables(lib_root)
    assert "symbol-case-collision" in codes(check.run(lib_root), check.ERROR)


def test_a_category_shadowing_an_official_library_is_a_warning(lib_root):
    kf.write_symbol(lib_root / "symbols" / "Amplifier_Audio.kicad_symdir" / "S.kicad_sym")
    tg.generate_tables(lib_root)
    report = check.run(lib_root)
    assert "category-shadows-official" in codes(report, check.WARNING)
    assert any("AX_Amplifier_Audio" in f.remedy for f in report.findings)
    assert report.exit_code == 0


def test_an_unparseable_file_is_an_error(lib_root):
    d = lib_root / "symbols" / "Amp.kicad_symdir"
    d.mkdir(parents=True)
    (d / "X.kicad_sym").write_text('(kicad_symbol_lib\n\t(symbol "X"\n')
    tg.generate_tables(lib_root)
    assert "file-unreadable" in codes(check.run(lib_root), check.ERROR)


# --------------------------------------------------------------------------
# Informational findings
# --------------------------------------------------------------------------

def test_orphans_are_notes_not_failures(lib_root):
    kf.write_footprint(lib_root / "footprints" / "Amp.pretty" / "LONELY.kicad_mod")
    kf.write_step(lib_root / "3dmodels" / "Amp.3dshapes" / "unused.step")
    tg.generate_tables(lib_root)
    report = check.run(lib_root)
    assert "footprint-orphan" in codes(report, check.INFO)
    assert "model-orphan" in codes(report, check.INFO)
    assert report.exit_code == 0


def test_a_symbol_without_a_footprint_is_only_a_note(lib_root):
    kf.write_symbol(lib_root / "symbols" / "Amp.kicad_symdir" / "S.kicad_sym", footprint="")
    tg.generate_tables(lib_root)
    report = check.run(lib_root)
    assert "symbol-no-footprint" in codes(report, check.INFO)
    assert report.exit_code == 0


def test_a_partial_category_is_only_a_note(lib_root):
    """A symbol-only library is perfectly normal."""
    kf.write_symbol(lib_root / "symbols" / "SymOnly.kicad_symdir" / "S.kicad_sym", footprint="")
    tg.generate_tables(lib_root)
    report = check.run(lib_root)
    assert "category-partial" in codes(report, check.INFO)
    assert report.exit_code == 0


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------

def test_a_stale_provenance_entry_is_a_warning(lib_root):
    _healthy(lib_root)
    prov = pv.load(lib_root)
    prov.record("Amp", pv.KIND_SYMBOL, "DELETED_LONG_AGO")
    prov.save()
    report = check.run(lib_root)
    assert "provenance-stale" in codes(report, check.WARNING)
    assert report.exit_code == 0


def test_a_corrupt_provenance_file_is_an_error(lib_root):
    _healthy(lib_root)
    (lib_root / "provenance.json").write_text("{broken")
    report = check.run(lib_root)
    assert "provenance-corrupt" in codes(report, check.ERROR)


def test_valid_provenance_produces_no_finding(lib_root):
    _healthy(lib_root)
    prov = pv.load(lib_root)
    prov.record("Amp", pv.KIND_SYMBOL, "TPA3255DDV")
    prov.record("Amp", pv.KIND_MODEL, "SOP63.step")
    prov.save()
    assert "provenance-stale" not in codes(check.run(lib_root))


# --------------------------------------------------------------------------
# kicad-cli integration
# --------------------------------------------------------------------------

def test_kicad_cli_is_optional(lib_root):
    _healthy(lib_root)
    report = check.run(lib_root, use_kicad_cli=False)
    assert report.kicad_cli_used is False
    assert "did not run" in report.summary()
    assert report.exit_code == 0


@pytest.mark.skipif(not HAS_CLI, reason="kicad-cli not on PATH")
def test_kicad_cli_validates_a_healthy_library(lib_root):
    report = check.run(_healthy(lib_root))
    assert report.kicad_cli_used is True
    assert report.errors == [], report.summary()


@pytest.mark.skipif(not HAS_CLI, reason="kicad-cli not on PATH")
def test_kicad_cli_catches_a_footprint_our_parser_accepts(lib_root):
    """
    A structurally balanced footprint that KiCad still rejects -- the reason
    the external validator is worth running at all.
    """
    _healthy(lib_root)
    (lib_root / "footprints" / "Amp.pretty" / "BAD.kicad_mod").write_text(
        '(footprint "BAD"\n\t(version 20260206)\n\t(nonsense_token "x")\n)\n'
    )
    tg.generate_tables(lib_root)
    report = check.run(lib_root)
    assert "footprint-unparseable" in codes(report, check.ERROR)


@pytest.mark.skipif(not HAS_CLI, reason="kicad-cli not on PATH")
def test_kicad_cli_symbol_check_does_not_modify_the_library(lib_root):
    """`sym upgrade` rewrites files, so it must run on a copy."""
    _healthy(lib_root)
    sym = lib_root / "symbols" / "Amp.kicad_symdir" / "TPA3255DDV.kicad_sym"
    before = sym.read_bytes()
    check.run(lib_root)
    assert sym.read_bytes() == before


# --------------------------------------------------------------------------
# Output shapes
# --------------------------------------------------------------------------

def test_findings_are_sorted_errors_first(lib_root):
    _healthy(lib_root)
    (lib_root / "symbols" / "Empty.kicad_symdir").mkdir()
    kf.write_symbol(
        lib_root / "symbols" / "Amp.kicad_symdir" / "Bad.kicad_sym", footprint="Amp:GONE"
    )
    report = check.run(lib_root)
    severities = [f.severity for f in report.sorted()]
    assert severities == sorted(severities, key=lambda s: check._ORDER[s])


def test_json_output_is_serialisable_and_complete(lib_root):
    _healthy(lib_root)
    (lib_root / "3dmodels" / "Amp.3dshapes" / "SOP63.step").unlink()
    payload = check.run(lib_root).to_json()
    text = json.dumps(payload)
    assert "model-file-missing" in text
    assert payload["counts"]["error"] >= 1
    assert set(payload["findings"][0]) == {"severity", "code", "message", "where", "remedy"}


def test_summary_mentions_every_finding(lib_root):
    _healthy(lib_root)
    (lib_root / "3dmodels" / "Amp.3dshapes" / "SOP63.step").unlink()
    report = check.run(lib_root)
    text = report.summary()
    for f in report.findings:
        assert f.code in text


def test_a_clean_report_says_so(lib_root):
    tg.generate_tables(lib_root)
    assert "Nothing to report." in check.run(lib_root).summary()
