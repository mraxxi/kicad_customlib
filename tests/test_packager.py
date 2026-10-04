"""Phase 1: packager.py -- a self-contained project bundle."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core import library as lb
from src.core import ops
from src.core import packager as pk
from src.core import s_expr as sx
from src.core import table_gen as tg
from src.core.packager import PackageError
from tests import kicad_fixtures as kf


@pytest.fixture
def custom_lib(lib_root: Path) -> Path:
    """
    A library where the symbol, footprint and model names all differ -- the
    case the old name-globbing packager silently failed on.
    """
    kf.write_symbol(
        lib_root / "symbols" / "Amp.kicad_symdir" / "TPA3255DDV.kicad_sym",
        footprint="Amp:SOP63P810X120-44N",
    )
    kf.write_footprint(
        lib_root / "footprints" / "Amp.pretty" / "SOP63P810X120-44N.kicad_mod",
        model="${KICAD_CUSTOM_LIB}/3dmodels/Amp.3dshapes/amp_body_rev2.step",
        offset=(0.0, 0.0, 1.5), rotate=(0, 0, 90),
    )
    kf.write_step(lib_root / "3dmodels" / "Amp.3dshapes" / "amp_body_rev2.step")
    tg.generate_tables(lib_root)
    return lib_root


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return kf.write_project(
        tmp_path / "myboard",
        symbols=[
            ("Amp:TPA3255DDV", "Amp:SOP63P810X120-44N"),
            ("Device:R", "Resistor_SMD:R_0603_1608Metric"),
        ],
        pcb_footprints=["Amp:SOP63P810X120-44N", "Resistor_SMD:R_0603_1608Metric"],
    )


def _package(custom_lib, project, out=None):
    plan, result = pk.plan_package(custom_lib, project, out)
    applied = ops.apply(plan)
    assert applied.ok, applied.summary(project)
    return plan, result


# --------------------------------------------------------------------------
# Project scanning
# --------------------------------------------------------------------------

def test_find_used_libraries_reads_schematics_and_boards(project):
    syms, fps = pk.find_used_libraries(project)
    assert syms == {"Amp:TPA3255DDV", "Device:R"}
    assert fps == {"Amp:SOP63P810X120-44N", "Resistor_SMD:R_0603_1608Metric"}


def test_a_footprint_only_on_the_board_is_still_found(tmp_path):
    """A board edited directly can reference a footprint the schematic does not."""
    proj = kf.write_project(tmp_path / "p", symbols=[], pcb_footprints=["Amp:BOARD_ONLY"])
    _syms, fps = pk.find_used_libraries(proj)
    assert "Amp:BOARD_ONLY" in fps


def test_a_project_without_a_board_works(tmp_path):
    proj = kf.write_project(tmp_path / "p", symbols=[("Amp:S", "Amp:F")])
    syms, fps = pk.find_used_libraries(proj)
    assert syms == {"Amp:S"} and fps == {"Amp:F"}


def test_scanning_a_non_directory_is_a_clear_error(tmp_path):
    with pytest.raises(PackageError, match="not a directory"):
        pk.find_used_libraries(tmp_path / "nope")


# --------------------------------------------------------------------------
# Models resolved from the footprint, not by globbing
# --------------------------------------------------------------------------

def test_a_model_is_packaged_even_when_its_name_matches_nothing(custom_lib, project):
    """
    Symbol TPA3255DDV, footprint SOP63P810X120-44N, model amp_body_rev2.step.
    The old packager globbed '<footprint>*.step' and so copied nothing.
    """
    _plan, result = _package(custom_lib, project)
    assert result.packaged_models == ["Amp/amp_body_rev2.step"]
    assert (project / "project_libs" / "3dmodels" / "Amp.3dshapes" / "amp_body_rev2.step").exists()


def test_the_packaged_footprint_points_at_the_bundled_model(custom_lib, project):
    _package(custom_lib, project)
    packaged = project / "project_libs" / "footprints" / "Amp.pretty" / "SOP63P810X120-44N.kicad_mod"
    models = sx.all_models(sx.read_text(packaged))
    assert [m.path for m in models] == [
        "${KIPRJMOD}/project_libs/3dmodels/Amp.3dshapes/amp_body_rev2.step"
    ]


def test_the_hand_tuned_alignment_survives_packaging(custom_lib, project):
    _package(custom_lib, project)
    packaged = project / "project_libs" / "footprints" / "Amp.pretty" / "SOP63P810X120-44N.kicad_mod"
    m = sx.all_models(sx.read_text(packaged))[0]
    assert (m.offset, m.rotate) == ((0.0, 0.0, 1.5), (0.0, 0.0, 90.0))


def test_a_footprint_with_several_models_packages_all_of_them(lib_root, tmp_path):
    kf.write_footprint(
        lib_root / "footprints" / "C.pretty" / "FP.kicad_mod",
        models=[
            "${KICAD_CUSTOM_LIB}/3dmodels/C.3dshapes/a.step",
            "${KICAD_CUSTOM_LIB}/3dmodels/C.3dshapes/b.wrl",
        ],
    )
    for n in ("a.step", "b.wrl"):
        kf.write_step(lib_root / "3dmodels" / "C.3dshapes" / n)
    proj = kf.write_project(tmp_path / "p", symbols=[], pcb_footprints=["C:FP"])
    _plan, result = _package(lib_root, proj)
    assert sorted(result.packaged_models) == ["C/a.step", "C/b.wrl"]
    packaged = proj / "project_libs" / "footprints" / "C.pretty" / "FP.kicad_mod"
    assert len(sx.all_models(sx.read_text(packaged))) == 2


def test_a_missing_model_is_warned_about_not_silently_skipped(lib_root, tmp_path):
    kf.write_footprint(
        lib_root / "footprints" / "C.pretty" / "FP.kicad_mod",
        model="${KICAD_CUSTOM_LIB}/3dmodels/C.3dshapes/gone.step",
    )
    proj = kf.write_project(tmp_path / "p", symbols=[], pcb_footprints=["C:FP"])
    plan, _result = pk.plan_package(lib_root, proj)
    assert any("references a model that is missing" in w for w in plan.warnings)


def test_a_footprint_without_a_model_packages_fine(lib_root, tmp_path):
    kf.write_footprint(lib_root / "footprints" / "C.pretty" / "FP.kicad_mod")
    proj = kf.write_project(tmp_path / "p", symbols=[], pcb_footprints=["C:FP"])
    _plan, result = _package(lib_root, proj)
    assert result.packaged_footprints == ["C:FP"]
    assert result.packaged_models == []


# --------------------------------------------------------------------------
# Tables at the project root, merged not replaced
# --------------------------------------------------------------------------

def test_tables_are_written_at_the_project_root(custom_lib, project):
    """KiCad reads project tables from the project root, not from a subfolder."""
    _package(custom_lib, project)
    assert (project / "sym-lib-table").exists()
    assert (project / "fp-lib-table").exists()
    assert not (project / "project_libs" / "sym-lib-table").exists()
    assert not (project / "project_libs" / "fp-lib-table").exists()


def test_table_uris_use_kiprjmod_and_the_actual_output_folder(custom_lib, project):
    _package(custom_lib, project)
    text = (project / "fp-lib-table").read_text()
    assert '(uri "${KIPRJMOD}/project_libs/footprints/Amp.pretty")' in text
    assert str(custom_lib) not in text


def test_an_existing_project_table_keeps_its_rows(custom_lib, project):
    """The old packager replaced the table and discarded configured libraries."""
    (project / "fp-lib-table").write_text(
        '(fp_lib_table\n'
        '\t(version 7)\n'
        '\t(lib (name "MyOtherLib") (type "KiCad") (uri "${KIPRJMOD}/other.pretty") (options "") (descr "hand made"))\n'
        ')\n'
    )
    _package(custom_lib, project)
    text = (project / "fp-lib-table").read_text()
    assert "MyOtherLib" in text
    assert "hand made" in text
    assert '(name "Amp")' in text


def test_an_existing_table_is_backed_up_before_being_changed(custom_lib, project):
    (project / "fp-lib-table").write_text(
        '(fp_lib_table\n\t(version 7)\n'
        '\t(lib (name "Keep") (type "KiCad") (uri "${KIPRJMOD}/k.pretty") (options "") (descr "k"))\n'
        ')\n'
    )
    _package(custom_lib, project)
    backup = project / "fp-lib-table.bak"
    assert backup.exists()
    assert "Amp" not in backup.read_text()


def test_a_table_already_listing_the_library_is_left_alone(custom_lib, project):
    _package(custom_lib, project)
    before = (project / "fp-lib-table").read_bytes()
    plan, _result = pk.plan_package(custom_lib, project)
    assert not any(op.target.name == "fp-lib-table" for op in plan.operations)
    assert any("already lists every packaged library" in n for n in plan.notes)
    assert (project / "fp-lib-table").read_bytes() == before


def test_merge_table_adds_only_missing_rows():
    existing = (
        '(fp_lib_table\n\t(version 7)\n'
        '\t(lib (name "A") (type "KiCad") (uri "u") (options "") (descr "d"))\n'
        ')\n'
    )
    entries = [tg.LibEntry("A", "u2", "d2"), tg.LibEntry("B", "u3", "d3")]
    merged, added = pk.merge_table(existing, "fp_lib_table", entries)
    assert added == ["B"]
    assert merged.count('(name "A")') == 1
    assert '(uri "u")' in merged          # the existing row is untouched
    assert '(name "B")' in merged


def test_merge_table_creates_a_fresh_table_when_none_exists():
    merged, added = pk.merge_table(None, "sym_lib_table", [tg.LibEntry("A", "u", "d")])
    assert merged.startswith("(sym_lib_table\n\t(version 7)\n")
    assert added == ["A"] and merged.endswith(")\n")


def test_merge_table_rejects_a_corrupt_existing_table():
    with pytest.raises(PackageError, match="not valid S-expression"):
        pk.merge_table("(fp_lib_table\n\t(version 7)\n", "fp_lib_table",
                       [tg.LibEntry("A", "u", "d")])


def test_existing_lib_names_reads_nicknames():
    text = (
        '(fp_lib_table\n\t(version 7)\n'
        '\t(lib (name "One") (type "KiCad") (uri "u") (options "") (descr "d"))\n'
        '\t(lib (name "Two") (type "KiCad") (uri "u") (options "") (descr "d"))\n'
        ')\n'
    )
    assert pk.existing_lib_names(text) == ["One", "Two"]


# --------------------------------------------------------------------------
# --out handling
# --------------------------------------------------------------------------

def test_a_custom_output_folder_produces_matching_kiprjmod_paths(custom_lib, project):
    _plan, result = _package(custom_lib, project, out=project / "libs" / "vendor")
    assert result.rel_out == "libs/vendor"
    text = (project / "fp-lib-table").read_text()
    assert '(uri "${KIPRJMOD}/libs/vendor/footprints/Amp.pretty")' in text
    packaged = project / "libs" / "vendor" / "footprints" / "Amp.pretty" / "SOP63P810X120-44N.kicad_mod"
    assert sx.all_models(sx.read_text(packaged))[0].path.startswith(
        "${KIPRJMOD}/libs/vendor/3dmodels/"
    )


def test_the_project_root_itself_is_a_valid_output_folder(custom_lib, project):
    _plan, result = _package(custom_lib, project, out=project)
    assert result.rel_out == ""
    assert '(uri "${KIPRJMOD}/footprints/Amp.pretty")' in (project / "fp-lib-table").read_text()


def test_an_output_folder_outside_the_project_is_refused(custom_lib, project, tmp_path):
    """${KIPRJMOD} cannot address anything outside the project."""
    with pytest.raises(PackageError, match="must be inside the project"):
        pk.plan_package(custom_lib, project, tmp_path / "elsewhere")


# --------------------------------------------------------------------------
# Unresolved items
# --------------------------------------------------------------------------

def test_official_library_parts_are_reported_not_packaged(custom_lib, project):
    _plan, result = _package(custom_lib, project)
    assert result.unresolved_symbols == ["Device:R"]
    assert result.unresolved_footprints == ["Resistor_SMD:R_0603_1608Metric"]
    assert not (project / "project_libs" / "symbols" / "Device.kicad_symdir").exists()


def test_the_summary_distinguishes_packaged_from_unresolved(custom_lib, project):
    _plan, result = _package(custom_lib, project)
    text = result.summary()
    assert "Symbols:    1" in text
    assert "Not in the custom library" in text
    assert "Device:R" in text


def test_a_project_using_nothing_custom_packages_nothing(custom_lib, tmp_path):
    proj = kf.write_project(
        tmp_path / "p", symbols=[("Device:R", "Resistor_SMD:R_0603_1608Metric")]
    )
    plan, result = pk.plan_package(custom_lib, proj)
    assert result.packaged_symbols == [] and result.packaged_footprints == []
    assert any("uses no components from this custom library" in n for n in plan.notes)
    assert not (proj / "sym-lib-table").exists()


# --------------------------------------------------------------------------
# Derived symbols
# --------------------------------------------------------------------------

def test_a_derived_symbols_parent_is_packaged_too(lib_root, tmp_path):
    """
    A project naming only the derived symbol still needs the parent, which
    lives in a sibling file the project never references.
    """
    d = lib_root / "symbols" / "Amp.kicad_symdir"
    kf.write_symbol(d / "TPA3255DDV.kicad_sym", footprint="Amp:FP")
    kf.write_symbol(d / "TPA3251DDV.kicad_sym", footprint="Amp:FP", extends="TPA3255DDV")
    kf.write_footprint(lib_root / "footprints" / "Amp.pretty" / "FP.kicad_mod")
    proj = kf.write_project(tmp_path / "p", symbols=[("Amp:TPA3251DDV", "Amp:FP")])

    plan, result = _package(lib_root, proj)
    assert sorted(result.packaged_symbols) == ["Amp:TPA3251DDV", "Amp:TPA3255DDV"]
    assert any("extends it" in n for n in plan.notes)
    out = proj / "project_libs" / "symbols" / "Amp.kicad_symdir"
    assert sorted(p.name for p in out.iterdir()) == [
        "TPA3251DDV.kicad_sym", "TPA3255DDV.kicad_sym",
    ]


def test_a_derived_symbol_with_a_missing_parent_warns(lib_root, tmp_path):
    d = lib_root / "symbols" / "Amp.kicad_symdir"
    kf.write_symbol(d / "D.kicad_sym", extends="ABSENT", footprint="Amp:FP")
    kf.write_footprint(lib_root / "footprints" / "Amp.pretty" / "FP.kicad_mod")
    proj = kf.write_project(tmp_path / "p", symbols=[("Amp:D", "Amp:FP")])
    plan, _result = pk.plan_package(lib_root, proj)
    assert any("will not load" in w for w in plan.warnings)


# --------------------------------------------------------------------------
# Hygiene
# --------------------------------------------------------------------------

def test_the_bundle_contains_no_absolute_paths(custom_lib, project):
    _package(custom_lib, project)
    for path in (project / "project_libs").rglob("*"):
        if path.is_file() and path.suffix in (".kicad_sym", ".kicad_mod"):
            assert str(custom_lib) not in path.read_text()
    for name in ("sym-lib-table", "fp-lib-table"):
        assert str(custom_lib) not in (project / name).read_text()


def test_packaging_a_nickname_that_shadows_an_official_library_warns(lib_root, tmp_path):
    kf.write_footprint(lib_root / "footprints" / "Connector.pretty" / "FP.kicad_mod")
    proj = kf.write_project(tmp_path / "p", symbols=[], pcb_footprints=["Connector:FP"])
    plan, _result = pk.plan_package(lib_root, proj)
    assert any("two libraries with one nickname" in w for w in plan.warnings)


def test_planning_a_package_writes_nothing(custom_lib, project):
    pk.plan_package(custom_lib, project)
    assert not (project / "project_libs").exists()
    assert not (project / "sym-lib-table").exists()


def test_packaged_files_use_lf(custom_lib, project):
    _package(custom_lib, project)
    for path in (project / "project_libs").rglob("*.kicad_mod"):
        assert b"\r" not in path.read_bytes()
    assert b"\r" not in (project / "fp-lib-table").read_bytes()


def test_the_legacy_wrapper_still_works(custom_lib, project):
    res = pk.ProjectPackager(custom_lib).package_for_project(project)
    assert res["packaged_symbols"] == ["Amp:TPA3255DDV"]
    assert res["packaged_models"] == ["Amp/amp_body_rev2.step"]
    assert res["unresolved_symbols"] == ["Device:R"]
