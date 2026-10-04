"""Phase 1: refactor.py -- renames and moves rewrite every reference."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core import library as lb
from src.core import ops
from src.core import provenance as pv
from src.core import refactor as rf
from src.core import s_expr as sx
from src.core.refactor import RefactorError
from tests import kicad_fixtures as kf


def _apply(plan: ops.Plan):
    result = ops.apply(plan)
    assert result.ok, result.summary(plan.root)
    return result


# --------------------------------------------------------------------------
# Rename a symbol
# --------------------------------------------------------------------------

def test_rename_symbol_renames_the_file_and_the_internal_names(populated_lib):
    _apply(rf.plan_rename_symbol(populated_lib, "Amp_Test", "TPA3255DDV", "TPA3255DDV_V2"))
    lib = lb.scan(populated_lib)
    assert lib.find_symbol("Amp_Test", "TPA3255DDV") is None
    sym = lib.find_symbol("Amp_Test", "TPA3255DDV_V2")
    assert sym is not None and sym.internal_name == "TPA3255DDV_V2"
    assert sym.name_matches_internal


def test_rename_symbol_rewrites_extends_in_sibling_files(populated_lib):
    """
    The cross-file case. TPA3251DDV lives in its own file and extends
    TPA3255DDV; an in-file-only rewrite would leave it pointing at a parent
    that no longer exists.
    """
    _apply(rf.plan_rename_symbol(populated_lib, "Amp_Test", "TPA3255DDV", "TPA3255DDV_V2"))
    lib = lb.scan(populated_lib)
    assert lib.find_symbol("Amp_Test", "TPA3251DDV").extends == "TPA3255DDV_V2"
    assert lib.resolve().dangling == []


def test_rename_symbol_reports_how_many_siblings_it_touched(populated_lib):
    plan = rf.plan_rename_symbol(populated_lib, "Amp_Test", "TPA3255DDV", "NEW")
    assert any("1 sibling file" in n for n in plan.notes)
    assert any("Amp_Test:TPA3251DDV" in n for n in plan.notes)


def test_rename_symbol_warns_that_projects_will_break(populated_lib):
    plan = rf.plan_rename_symbol(populated_lib, "Amp_Test", "TPA3255DDV", "NEW")
    assert any("will break" in w and "Edit Symbol Library References" in w
               for w in plan.warnings)


def test_renamed_symbol_still_loads_in_kicad(populated_lib):
    import shutil, subprocess
    cli = shutil.which("kicad-cli")
    if cli is None:
        pytest.skip("kicad-cli not on PATH")
    _apply(rf.plan_rename_symbol(populated_lib, "Amp_Test", "TPA3255DDV", "RENAMED"))
    out = populated_lib / "svg"
    out.mkdir()
    r = subprocess.run(
        [cli, "sym", "export", "svg", "-o", str(out),
         str(populated_lib / "symbols" / "Amp_Test.kicad_symdir")],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    # Both the renamed parent and the derived sibling must still plot.
    assert len(list(out.glob("*.svg"))) == 2


def test_rename_symbol_refuses_an_existing_target(populated_lib):
    with pytest.raises(RefactorError, match="already exists"):
        rf.plan_rename_symbol(populated_lib, "Amp_Test", "TPA3255DDV", "TPA3251DDV")


def test_rename_symbol_rejects_an_invalid_name(populated_lib):
    with pytest.raises(Exception):
        rf.plan_rename_symbol(populated_lib, "Amp_Test", "TPA3255DDV", "Bad:Name")


def test_rename_symbol_rejects_an_unknown_symbol(populated_lib):
    with pytest.raises(RefactorError, match="no symbol"):
        rf.plan_rename_symbol(populated_lib, "Amp_Test", "NOPE", "X")


def test_renaming_to_the_same_name_is_a_no_op(populated_lib):
    assert rf.plan_rename_symbol(
        populated_lib, "Amp_Test", "TPA3255DDV", "TPA3255DDV"
    ).is_empty


def test_rename_symbol_updates_provenance(populated_lib):
    prov = pv.load(populated_lib)
    prov.record("Amp_Test", pv.KIND_SYMBOL, "TPA3255DDV", source="SnapEDA")
    plan = rf.plan_rename_symbol(populated_lib, "Amp_Test", "TPA3255DDV", "NEW")
    pv.apply_edits(prov, plan.provenance)
    assert prov.get("Amp_Test", pv.KIND_SYMBOL, "TPA3255DDV") is None
    assert prov.get("Amp_Test", pv.KIND_SYMBOL, "NEW").source == "SnapEDA"


# --------------------------------------------------------------------------
# Rename a footprint
# --------------------------------------------------------------------------

def test_rename_footprint_updates_every_referencing_symbol(populated_lib):
    _apply(rf.plan_rename_footprint(
        populated_lib, "Amp_Test", "SOP63P810X120-44N", "SOP65P810X120-44N"
    ))
    lib = lb.scan(populated_lib)
    assert lib.find_footprint("Amp_Test", "SOP65P810X120-44N").internal_name == "SOP65P810X120-44N"
    for name in ("TPA3255DDV", "TPA3251DDV"):
        assert lib.find_symbol("Amp_Test", name).footprint_ref == "Amp_Test:SOP65P810X120-44N"
    assert lib.resolve().dangling == []


def test_rename_footprint_reports_the_symbols_it_updated(populated_lib):
    plan = rf.plan_rename_footprint(populated_lib, "Amp_Test", "SOP63P810X120-44N", "NEW")
    assert any("2 symbol(s)" in n for n in plan.notes)


def test_rename_footprint_across_categories(lib_root):
    """A symbol in one category referencing a footprint in another."""
    kf.write_symbol(lib_root / "symbols" / "A.kicad_symdir" / "S.kicad_sym", footprint="B:FP")
    kf.write_footprint(lib_root / "footprints" / "B.pretty" / "FP.kicad_mod")
    _apply(rf.plan_rename_footprint(lib_root, "B", "FP", "FP_NEW"))
    lib = lb.scan(lib_root)
    assert lib.find_symbol("A", "S").footprint_ref == "B:FP_NEW"
    assert lib.resolve().dangling == []


def test_rename_footprint_can_rename_the_model_to_match(populated_lib):
    _apply(rf.plan_rename_footprint(
        populated_lib, "Amp_Test", "SOP63P810X120-44N", "NEW_FP", rename_model=True
    ))
    lib = lb.scan(populated_lib)
    assert [m.filename for m in lib.models_in("Amp_Test")] == ["NEW_FP.step"]
    assert lib.find_footprint("Amp_Test", "NEW_FP").model_paths == (
        "${KICAD_CUSTOM_LIB}/3dmodels/Amp_Test.3dshapes/NEW_FP.step",
    )
    assert lib.resolve().dangling == []


def test_rename_footprint_leaves_the_model_alone_by_default(populated_lib):
    _apply(rf.plan_rename_footprint(populated_lib, "Amp_Test", "SOP63P810X120-44N", "NEW_FP"))
    lib = lb.scan(populated_lib)
    assert [m.filename for m in lib.models_in("Amp_Test")] == ["TPA3255DDV.step"]
    assert lib.resolve().dangling == []


def test_rename_footprint_notes_when_nothing_referenced_it(lib_root):
    kf.write_footprint(lib_root / "footprints" / "C.pretty" / "LONELY.kicad_mod")
    plan = rf.plan_rename_footprint(lib_root, "C", "LONELY", "STILL_LONELY")
    assert any("no symbol" in n for n in plan.notes)


# --------------------------------------------------------------------------
# Rename a model
# --------------------------------------------------------------------------

def test_rename_model_repoints_every_footprint(populated_lib):
    _apply(rf.plan_rename_model(populated_lib, "Amp_Test", "TPA3255DDV.step", "amp_body.step"))
    lib = lb.scan(populated_lib)
    assert lib.find_model("Amp_Test", "amp_body.step") is not None
    assert lib.find_footprint("Amp_Test", "SOP63P810X120-44N").model_paths == (
        "${KICAD_CUSTOM_LIB}/3dmodels/Amp_Test.3dshapes/amp_body.step",
    )
    assert lib.resolve().dangling == []


def test_rename_model_keeps_the_extension_when_omitted(populated_lib):
    _apply(rf.plan_rename_model(populated_lib, "Amp_Test", "TPA3255DDV.step", "amp_body"))
    assert lb.scan(populated_lib).find_model("Amp_Test", "amp_body.step") is not None


def test_rename_model_preserves_the_offset_in_the_footprint(lib_root):
    kf.write_footprint(
        lib_root / "footprints" / "C.pretty" / "FP.kicad_mod",
        model="${KICAD_CUSTOM_LIB}/3dmodels/C.3dshapes/m.step",
        offset=(1.0, 2.0, 3.0), rotate=(0, 0, 90),
    )
    kf.write_step(lib_root / "3dmodels" / "C.3dshapes" / "m.step")
    _apply(rf.plan_rename_model(lib_root, "C", "m.step", "renamed.step"))
    text = sx.read_text(lb.scan(lib_root).find_footprint("C", "FP").path)
    m = sx.all_models(text)[0]
    assert (m.offset, m.rotate) == ((1.0, 2.0, 3.0), (0.0, 0.0, 90.0))


# --------------------------------------------------------------------------
# Move between categories
# --------------------------------------------------------------------------

def test_move_footprint_updates_referencing_symbols(populated_lib):
    _apply(rf.plan_move(
        populated_lib, rf.KIND_FOOTPRINT, "Amp_Test", "SOP63P810X120-44N", "Pkg_SOP"
    ))
    lib = lb.scan(populated_lib)
    assert lib.find_footprint("Pkg_SOP", "SOP63P810X120-44N") is not None
    assert lib.find_footprint("Amp_Test", "SOP63P810X120-44N") is None
    for name in ("TPA3255DDV", "TPA3251DDV"):
        assert lib.find_symbol("Amp_Test", name).footprint_ref == "Pkg_SOP:SOP63P810X120-44N"


def test_move_footprint_warns_when_its_model_stays_behind(populated_lib):
    plan = rf.plan_move(
        populated_lib, rf.KIND_FOOTPRINT, "Amp_Test", "SOP63P810X120-44N", "Pkg_SOP"
    )
    assert any("still points at its model in the old category" in w for w in plan.warnings)


def test_move_model_repoints_its_footprints(populated_lib):
    _apply(rf.plan_move(populated_lib, rf.KIND_MODEL, "Amp_Test", "TPA3255DDV.step", "Shared_3D"))
    lib = lb.scan(populated_lib)
    assert lib.find_model("Shared_3D", "TPA3255DDV.step") is not None
    assert lib.find_footprint("Amp_Test", "SOP63P810X120-44N").model_paths == (
        "${KICAD_CUSTOM_LIB}/3dmodels/Shared_3D.3dshapes/TPA3255DDV.step",
    )
    assert lib.resolve().dangling == []


def test_moving_a_parent_symbol_out_of_its_symdir_is_refused(populated_lib):
    """
    A derived symbol's parent must be a sibling file in the same symdir, so
    moving the parent away would leave TPA3251DDV unloadable.
    """
    with pytest.raises(RefactorError, match="parent of 1 derived symbol"):
        rf.plan_move(populated_lib, rf.KIND_SYMBOL, "Amp_Test", "TPA3255DDV", "Other")


def test_moving_a_derived_symbol_away_from_its_parent_warns(populated_lib):
    plan = rf.plan_move(populated_lib, rf.KIND_SYMBOL, "Amp_Test", "TPA3251DDV", "Other")
    assert any("extends 'TPA3255DDV'" in w for w in plan.warnings)


def test_move_a_plain_symbol_succeeds(populated_lib):
    _apply(rf.plan_move(populated_lib, rf.KIND_SYMBOL, "Conn_Test", "KF2EDG", "Conn_New"))
    lib = lb.scan(populated_lib)
    assert lib.find_symbol("Conn_New", "KF2EDG") is not None
    assert lib.find_symbol("Conn_Test", "KF2EDG") is None


def test_move_removes_the_emptied_source_directory(populated_lib):
    _apply(rf.plan_move(populated_lib, rf.KIND_SYMBOL, "Conn_Test", "KF2EDG", "Conn_New"))
    assert not (populated_lib / "symbols" / "Conn_Test.kicad_symdir").exists()


def test_move_leaves_a_still_populated_source_directory_alone(populated_lib):
    _apply(rf.plan_move(populated_lib, rf.KIND_SYMBOL, "Amp_Test", "TPA3251DDV", "Other"))
    assert (populated_lib / "symbols" / "Amp_Test.kicad_symdir").is_dir()


def test_move_to_the_same_category_is_a_no_op(populated_lib):
    assert rf.plan_move(
        populated_lib, rf.KIND_SYMBOL, "Conn_Test", "KF2EDG", "Conn_Test"
    ).is_empty


def test_move_rejects_an_unknown_kind(populated_lib):
    with pytest.raises(RefactorError, match="unknown kind"):
        rf.plan_move(populated_lib, "gerber", "Amp_Test", "x", "y")


def test_move_updates_provenance_category(populated_lib):
    prov = pv.load(populated_lib)
    prov.record("Conn_Test", pv.KIND_SYMBOL, "KF2EDG", source="vendor")
    plan = rf.plan_move(
        populated_lib, rf.KIND_SYMBOL, "Conn_Test", "KF2EDG", "Conn_New"
    )
    pv.apply_edits(prov, plan.provenance)
    assert prov.get("Conn_New", pv.KIND_SYMBOL, "KF2EDG").source == "vendor"


# --------------------------------------------------------------------------
# Rename a category
# --------------------------------------------------------------------------

def test_rename_category_moves_all_three_directories(populated_lib):
    _apply(rf.plan_rename_category(populated_lib, "Amp_Test", "TI_Amps"))
    lib = lb.scan(populated_lib)
    assert "Amp_Test" not in lib.categories
    cat = lib.categories["TI_Amps"]
    assert (cat.symbol_count, cat.footprint_count, cat.model_count) == (2, 1, 1)


def test_rename_category_rewrites_lib_id_prefixes_and_model_paths(populated_lib):
    _apply(rf.plan_rename_category(populated_lib, "Amp_Test", "TI_Amps"))
    lib = lb.scan(populated_lib)
    for name in ("TPA3255DDV", "TPA3251DDV"):
        assert lib.find_symbol("TI_Amps", name).footprint_ref == "TI_Amps:SOP63P810X120-44N"
    assert lib.find_footprint("TI_Amps", "SOP63P810X120-44N").model_paths == (
        "${KICAD_CUSTOM_LIB}/3dmodels/TI_Amps.3dshapes/TPA3255DDV.step",
    )
    assert lib.resolve().dangling == []


def test_rename_category_keeps_derived_symbols_resolvable(populated_lib):
    _apply(rf.plan_rename_category(populated_lib, "Amp_Test", "TI_Amps"))
    lib = lb.scan(populated_lib)
    assert lib.find_symbol("TI_Amps", "TPA3251DDV").extends == "TPA3255DDV"
    assert lib.resolve().dangling == []


def test_rename_category_leaves_other_categories_untouched(populated_lib):
    _apply(rf.plan_rename_category(populated_lib, "Amp_Test", "TI_Amps"))
    lib = lb.scan(populated_lib)
    assert lib.find_symbol("Conn_Test", "KF2EDG").footprint_ref == "Conn_Test:KF2EDG-5.08_4P"


def test_rename_category_removes_the_old_directories(populated_lib):
    _apply(rf.plan_rename_category(populated_lib, "Amp_Test", "TI_Amps"))
    for sub, suffix in (("symbols", ".kicad_symdir"),
                        ("footprints", ".pretty"),
                        ("3dmodels", ".3dshapes")):
        assert not (populated_lib / sub / f"Amp_Test{suffix}").exists()


def test_rename_category_warns_the_nickname_change_breaks_projects(populated_lib):
    plan = rf.plan_rename_category(populated_lib, "Amp_Test", "TI_Amps")
    assert any("library nickname changes" in w for w in plan.warnings)


def test_rename_category_refuses_to_merge_into_an_existing_one(populated_lib):
    with pytest.raises(RefactorError, match="already exists"):
        rf.plan_rename_category(populated_lib, "Amp_Test", "Conn_Test")


def test_rename_category_rejects_an_unknown_category(populated_lib):
    with pytest.raises(RefactorError, match="no category"):
        rf.plan_rename_category(populated_lib, "Nope", "X")


def test_rename_category_warns_about_an_official_collision(populated_lib):
    plan = rf.plan_rename_category(populated_lib, "Amp_Test", "Amplifier_Audio")
    assert any("official KiCad library" in w for w in plan.warnings)


def test_rename_category_updates_provenance_keys(populated_lib):
    prov = pv.load(populated_lib)
    prov.record("Amp_Test", pv.KIND_SYMBOL, "TPA3255DDV")
    prov.record("Amp_Test", pv.KIND_FOOTPRINT, "SOP63P810X120-44N")
    plan = rf.plan_rename_category(populated_lib, "Amp_Test", "TI_Amps")
    pv.apply_edits(prov, plan.provenance)
    assert prov.keys_for_category("Amp_Test") == []
    assert len(prov.keys_for_category("TI_Amps")) == 2


# --------------------------------------------------------------------------
# Planning never writes
# --------------------------------------------------------------------------

def test_planning_a_refactor_writes_nothing(populated_lib):
    before = {p: p.read_bytes() for p in populated_lib.rglob("*") if p.is_file()}
    rf.plan_rename_category(populated_lib, "Amp_Test", "TI_Amps")
    rf.plan_rename_symbol(populated_lib, "Amp_Test", "TPA3255DDV", "X")
    rf.plan_rename_footprint(populated_lib, "Amp_Test", "SOP63P810X120-44N", "Y")
    after = {p: p.read_bytes() for p in populated_lib.rglob("*") if p.is_file()}
    assert before == after


def test_a_file_touched_twice_keeps_both_changes(lib_root):
    """
    A symbol that is itself renamed *and* whose footprint reference is
    rewritten must end up with both edits -- two independent writes to the
    same path would lose the first.
    """
    d = lib_root / "symbols" / "C.kicad_symdir"
    kf.write_symbol(d / "OLD_SYM.kicad_sym", footprint="C:OLD_FP")
    kf.write_footprint(lib_root / "footprints" / "C.pretty" / "OLD_FP.kicad_mod")

    _apply(rf.plan_rename_footprint(lib_root, "C", "OLD_FP", "NEW_FP"))
    _apply(rf.plan_rename_symbol(lib_root, "C", "OLD_SYM", "NEW_SYM"))

    lib = lb.scan(lib_root)
    sym = lib.find_symbol("C", "NEW_SYM")
    assert sym is not None
    assert sym.internal_name == "NEW_SYM"
    assert sym.footprint_ref == "C:NEW_FP"
    assert lib.resolve().dangling == []
