"""Phase 1: library.py -- the disk is the source of truth."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core import library as lb
from tests import kicad_fixtures as kf


# --------------------------------------------------------------------------
# Categories and emptiness
# --------------------------------------------------------------------------

def test_scan_of_an_empty_root_finds_nothing(lib_root):
    lib = lb.scan(lib_root)
    assert lib.categories == {}
    assert (lib.symbols, lib.footprints, lib.models) == ([], [], [])


def test_empty_category_directories_are_reported_but_hold_nothing(lib_root):
    """
    The exact state this repo accumulated: two categories whose six mirror
    directories all exist and are empty, so the generated tables advertise
    libraries that contain no parts -- and that git cannot even sync, because
    it does not track empty directories.
    """
    for cat in ("3255", "TI-TPAxxx_AUDIO-AMP"):
        (lib_root / "symbols" / f"{cat}.kicad_symdir").mkdir()
        (lib_root / "footprints" / f"{cat}.pretty").mkdir()
        (lib_root / "3dmodels" / f"{cat}.3dshapes").mkdir()

    lib = lb.scan(lib_root)
    assert lib.category_names() == ["3255", "TI-TPAxxx_AUDIO-AMP"]
    assert lib.non_empty_categories() == []
    assert all(c.is_empty for c in lib.categories.values())


def test_partial_mirroring_is_detected(lib_root):
    (lib_root / "symbols" / "Half.kicad_symdir").mkdir()
    kf.write_symbol(lib_root / "symbols" / "Half.kicad_symdir" / "S.kicad_sym")
    cat = lb.scan(lib_root).categories["Half"]
    assert cat.has_symbol_dir and not cat.has_footprint_dir and not cat.has_model_dir
    assert cat.missing_mirrors == ["footprints/Half.pretty", "3dmodels/Half.3dshapes"]
    assert not cat.is_empty


def test_scan_counts_each_kind(populated_lib):
    lib = lb.scan(populated_lib)
    amp = lib.categories["Amp_Test"]
    assert (amp.symbol_count, amp.footprint_count, amp.model_count) == (2, 1, 1)
    conn = lib.categories["Conn_Test"]
    assert (conn.symbol_count, conn.footprint_count, conn.model_count) == (1, 1, 0)


def test_ordering_is_deterministic_and_case_insensitive(lib_root):
    d = lib_root / "symbols" / "Cat.kicad_symdir"
    for name in ("beta", "Alpha", "alpha2", "Beta2"):
        kf.write_symbol(d / f"{name}.kicad_sym")
    names = [s.name for s in lb.scan(lib_root).symbols]
    assert names == ["Alpha", "alpha2", "beta", "Beta2"]


def test_scan_is_read_only(populated_lib):
    before = {p: p.stat().st_mtime_ns for p in populated_lib.rglob("*") if p.is_file()}
    lb.scan(populated_lib).resolve()
    after = {p: p.stat().st_mtime_ns for p in populated_lib.rglob("*") if p.is_file()}
    assert before == after


# --------------------------------------------------------------------------
# Symbol records
# --------------------------------------------------------------------------

def test_symbol_records_lib_id_and_footprint_ref(populated_lib):
    lib = lb.scan(populated_lib)
    sym = lib.find_symbol("Amp_Test", "TPA3255DDV")
    assert sym.lib_id == "Amp_Test:TPA3255DDV"
    assert sym.footprint_ref == "Amp_Test:SOP63P810X120-44N"
    assert sym.internal_name == "TPA3255DDV"
    assert sym.name_matches_internal


def test_derived_symbol_records_its_parent(populated_lib):
    sym = lb.scan(populated_lib).find_symbol("Amp_Test", "TPA3251DDV")
    assert sym.extends == "TPA3255DDV"


def test_case_only_name_mismatch_is_flagged_separately(lib_root):
    """Mirrors the real Interface_USB/tusb564.kicad_sym -> (symbol "TUSB564")."""
    d = lib_root / "symbols" / "Iface.kicad_symdir"
    kf.write_symbol(d / "tusb564.kicad_sym", name="TUSB564")
    sym = lb.scan(lib_root).find_symbol("Iface", "tusb564")
    assert not sym.name_matches_internal
    assert sym.name_differs_only_by_case


def test_extra_top_level_symbols_are_recorded(lib_root):
    """A multi-symbol file is legal to read but violates the 1-per-file rule."""
    d = lib_root / "symbols" / "Cache.kicad_symdir"
    kf.write_multi_symbol(d / "TPA3255DDV.kicad_sym", ["TPA3255DDV", "CONN-5MM-4P", "KF2EDG"])
    sym = lb.scan(lib_root).find_symbol("Cache", "TPA3255DDV")
    assert sym.extra_symbols == ("CONN-5MM-4P", "KF2EDG")


def test_a_malformed_file_is_recorded_not_raised(lib_root):
    d = lib_root / "symbols" / "Broken.kicad_symdir"
    d.mkdir(parents=True)
    (d / "X.kicad_sym").write_text('(kicad_symbol_lib\n\t(symbol "X"\n')
    lib = lb.scan(lib_root)
    assert len(lib.unreadable) == 1
    assert "malformed" in lib.unreadable[0][1]
    assert lib.find_symbol("Broken", "X").error


# --------------------------------------------------------------------------
# Model path classification
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,kind", [
    ("${KICAD_CUSTOM_LIB}/3dmodels/C.3dshapes/M.step", "custom_lib"),
    ("${KIPRJMOD}/project_libs/3dmodels/C.3dshapes/M.step", "other_var"),
    ("/home/archvan/models/M.step", "absolute"),
    ("C:/Users/x/M.step", "absolute"),
    ("../models/M.step", "relative"),
])
def test_classify_model_path(raw, kind, tmp_path):
    assert lb.classify_model_path(tmp_path, raw).kind == kind


def test_classify_model_path_resolves_against_the_library_root(tmp_path):
    info = lb.classify_model_path(tmp_path, "${KICAD_CUSTOM_LIB}/3dmodels/C.3dshapes/M.step")
    assert info.resolved == tmp_path / "3dmodels/C.3dshapes/M.step"
    assert info.filename == "M.step"
    assert info.is_canonical


def test_classify_model_path_handles_backslashes(tmp_path):
    info = lb.classify_model_path(tmp_path, r"${KICAD_CUSTOM_LIB}\3dmodels\C.3dshapes\M.step")
    assert info.kind == "custom_lib" and info.filename == "M.step"


# --------------------------------------------------------------------------
# Reference resolution
# --------------------------------------------------------------------------

def test_resolve_links_symbol_to_footprint_to_model(populated_lib):
    refs = lb.scan(populated_lib).resolve()
    assert refs.symbol_to_footprint["Amp_Test:TPA3255DDV"] == "Amp_Test:SOP63P810X120-44N"
    assert refs.footprint_to_models["Amp_Test:SOP63P810X120-44N"] == [
        "3dmodels/Amp_Test.3dshapes/TPA3255DDV.step"
    ]
    assert refs.dangling == []


def test_two_symbols_may_share_one_footprint(populated_lib):
    """A part is not 1:1:1 -- both amp symbols point at the same footprint."""
    refs = lb.scan(populated_lib).resolve()
    assert sorted(refs.footprint_users["Amp_Test:SOP63P810X120-44N"]) == [
        "Amp_Test:TPA3251DDV", "Amp_Test:TPA3255DDV",
    ]


def test_a_footprint_may_carry_several_models(lib_root):
    kf.write_footprint(
        lib_root / "footprints" / "C.pretty" / "FP.kicad_mod",
        models=[
            "${KICAD_CUSTOM_LIB}/3dmodels/C.3dshapes/a.step",
            "${KICAD_CUSTOM_LIB}/3dmodels/C.3dshapes/b.wrl",
        ],
    )
    for n in ("a.step", "b.wrl"):
        kf.write_step(lib_root / "3dmodels" / "C.3dshapes" / n)
    refs = lb.scan(lib_root).resolve()
    assert len(refs.footprint_to_models["C:FP"]) == 2
    assert refs.dangling == []


def test_dangling_footprint_reference_is_reported(lib_root):
    kf.write_symbol(
        lib_root / "symbols" / "C.kicad_symdir" / "S.kicad_sym",
        footprint="C:DOES_NOT_EXIST",
    )
    refs = lb.scan(lib_root).resolve()
    assert [(d.kind, d.source, d.target) for d in refs.dangling] == [
        ("symbol_footprint", "C:S", "C:DOES_NOT_EXIST")
    ]


def test_footprint_reference_may_cross_categories(lib_root):
    """A symbol in category A legitimately referencing a footprint in B."""
    kf.write_symbol(lib_root / "symbols" / "A.kicad_symdir" / "S.kicad_sym", footprint="B:FP")
    kf.write_footprint(lib_root / "footprints" / "B.pretty" / "FP.kicad_mod")
    refs = lb.scan(lib_root).resolve()
    assert refs.dangling == []
    assert refs.footprint_users["B:FP"] == ["A:S"]


def test_missing_model_file_is_reported(lib_root):
    kf.write_footprint(
        lib_root / "footprints" / "C.pretty" / "FP.kicad_mod",
        model="${KICAD_CUSTOM_LIB}/3dmodels/C.3dshapes/gone.step",
    )
    refs = lb.scan(lib_root).resolve()
    assert refs.dangling[0].kind == "footprint_model"
    assert "does not exist" in refs.dangling[0].detail


def test_absolute_model_path_is_reported_as_non_canonical(lib_root):
    kf.write_footprint(
        lib_root / "footprints" / "C.pretty" / "FP.kicad_mod",
        model="/home/archvan/models/M.step",
    )
    refs = lb.scan(lib_root).resolve()
    assert refs.dangling[0].kind == "footprint_model"
    assert "absolute" in refs.dangling[0].detail


def test_symbol_without_a_footprint_property_is_unlinked_not_dangling(lib_root):
    kf.write_symbol(lib_root / "symbols" / "C.kicad_symdir" / "S.kicad_sym", footprint="")
    refs = lb.scan(lib_root).resolve()
    assert refs.unlinked_symbols == ["C:S"]
    assert refs.dangling == []


def test_derived_symbol_with_a_missing_parent_is_reported(lib_root):
    kf.write_symbol(
        lib_root / "symbols" / "C.kicad_symdir" / "D.kicad_sym", extends="MISSING_PARENT"
    )
    refs = lb.scan(lib_root).resolve()
    assert refs.dangling[0].kind == "symbol_extends"
    assert refs.dangling[0].target == "MISSING_PARENT"


def test_orphan_footprints_and_models_are_listed(lib_root):
    kf.write_footprint(lib_root / "footprints" / "C.pretty" / "LONELY.kicad_mod")
    kf.write_step(lib_root / "3dmodels" / "C.3dshapes" / "unused.step")
    refs = lb.scan(lib_root).resolve()
    assert refs.orphan_footprints == ["C:LONELY"]
    assert refs.orphan_models == ["3dmodels/C.3dshapes/unused.step"]


def test_model_rel_uri_is_the_canonical_form(populated_lib):
    model = lb.scan(populated_lib).find_model("Amp_Test", "TPA3255DDV.step")
    assert model.rel_uri == "${KICAD_CUSTOM_LIB}/3dmodels/Amp_Test.3dshapes/TPA3255DDV.step"


def test_all_model_extensions_are_recognised(lib_root):
    d = lib_root / "3dmodels" / "C.3dshapes"
    for n in ("a.step", "b.stp", "c.wrl", "d.STEP", "notes.txt"):
        kf.write_step(d / n)
    assert sorted(m.filename for m in lb.scan(lib_root).models) == [
        "a.step", "b.stp", "c.wrl", "d.STEP",
    ]
