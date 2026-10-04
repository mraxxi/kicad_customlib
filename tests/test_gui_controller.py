"""Phase 3: the GUI's view-model, tested without a display."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core import library as lb
from src.core import provenance as pv
from src.core import table_gen as tg
from src.gui import controller as ct
from tests import kicad_fixtures as kf


@pytest.fixture
def control(populated_lib) -> ct.Controller:
    tg.generate_tables(populated_lib)
    return ct.Controller(populated_lib)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

def test_iids_round_trip():
    iid = ct.make_iid("symbol", "Amp_Test", "TPA3255DDV")
    assert ct.parse_iid(iid) == ("symbol", "Amp_Test", "TPA3255DDV")


def test_a_numeric_looking_name_survives_as_a_string():
    """
    Tk turns a value like '3255' into an int when it is read back out of a
    Treeview's displayed values, which is what broke the old lookup. An
    explicit string iid cannot do that.
    """
    iid = ct.make_iid("symbol", "3255", "3255")
    kind, category, name = ct.parse_iid(iid)
    assert isinstance(category, str) and isinstance(name, str)
    assert (category, name) == ("3255", "3255")


def test_a_name_containing_a_space_and_paren_is_preserved():
    """The old GUI parsed the category out of 'Name (3)' with split(' (')."""
    iid = ct.make_iid("symbol", "Mini-Fit (MX4.2)", "Part (rev B)")
    assert ct.parse_iid(iid) == ("symbol", "Mini-Fit (MX4.2)", "Part (rev B)")


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------

def test_categories_are_listed_in_order(control):
    assert control.categories() == ["Amp_Test", "Conn_Test"]


def test_category_label_shows_counts(control):
    assert control.category_label("Amp_Test") == "Amp_Test  (2 sym, 1 fp, 1 3d)"


def test_category_label_marks_an_empty_category(lib_root):
    (lib_root / "symbols" / "3255.kicad_symdir").mkdir()
    assert ct.Controller(lib_root).category_label("3255") == "3255  (empty)"


def test_counts_summary(control):
    assert control.counts_summary() == (
        "3 symbols, 2 footprints, 1 3D models in 2 categories"
    )


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------

def test_symbol_rows_show_the_footprint_and_3d_state(control):
    rows = {r.name: r for r in control.rows(ct.KIND_SYMBOL)}
    assert rows["TPA3255DDV"].values[2] == "Amp_Test:SOP63P810X120-44N"
    assert rows["TPA3255DDV"].values[3] == ct.OK
    # KF2EDG's footprint exists but has no model
    assert rows["KF2EDG"].values[3] == ct.MISSING


def test_footprint_rows_count_models_and_users(control):
    row = next(r for r in control.rows(ct.KIND_FOOTPRINT) if r.name == "SOP63P810X120-44N")
    assert row.values[2] == "1"      # one model
    assert row.values[3] == "2"      # two symbols use it


def test_model_rows_count_users_and_show_a_size(control):
    row = control.rows(ct.KIND_MODEL)[0]
    assert row.values[2] == "1"
    assert row.values[3].endswith("B") or row.values[3].endswith("KB")


def test_rows_can_be_filtered_by_category(control):
    assert {r.name for r in control.rows(ct.KIND_SYMBOL, category="Conn_Test")} == {
        "KF2EDG"
    }


def test_search_matches_any_column(control):
    assert [r.name for r in control.rows(ct.KIND_SYMBOL, search="SOP63")] == [
        "TPA3251DDV", "TPA3255DDV",
    ]
    assert [r.name for r in control.rows(ct.KIND_SYMBOL, search="kf2")] == ["KF2EDG"]


def test_search_is_case_insensitive(control):
    assert control.rows(ct.KIND_SYMBOL, search="tpa3255") == \
        control.rows(ct.KIND_SYMBOL, search="TPA3255")


def test_search_with_no_match_is_empty(control):
    assert control.rows(ct.KIND_SYMBOL, search="zzzz") == []


def test_an_unknown_kind_is_rejected(control):
    with pytest.raises(ValueError, match="unknown kind"):
        control.rows("gerber")


def test_a_broken_reference_is_marked_as_a_problem(lib_root):
    kf.write_symbol(
        lib_root / "symbols" / "C.kicad_symdir" / "S.kicad_sym", footprint="C:GONE"
    )
    row = ct.Controller(lib_root).rows(ct.KIND_SYMBOL)[0]
    assert row.problem


def test_an_orphan_model_is_marked_as_a_problem(lib_root):
    kf.write_step(lib_root / "3dmodels" / "C.3dshapes" / "unused.step")
    row = ct.Controller(lib_root).rows(ct.KIND_MODEL)[0]
    assert "not referenced" in row.problem


def test_rows_carry_the_source_from_provenance(control):
    control.prov.record("Amp_Test", pv.KIND_SYMBOL, "TPA3255DDV", source="SnapEDA zip")
    row = next(r for r in control.rows(ct.KIND_SYMBOL) if r.name == "TPA3255DDV")
    assert row.values[4] == "SnapEDA zip"


# --------------------------------------------------------------------------
# Details
# --------------------------------------------------------------------------

def test_details_for_a_symbol_mentions_its_footprint(control):
    row = next(r for r in control.rows(ct.KIND_SYMBOL) if r.name == "TPA3255DDV")
    text = control.details(row)
    assert "Footprint: Amp_Test:SOP63P810X120-44N" in text
    assert "symbols/Amp_Test.kicad_symdir/TPA3255DDV.kicad_sym" in text


def test_details_for_a_derived_symbol_names_its_parent(control):
    row = next(r for r in control.rows(ct.KIND_SYMBOL) if r.name == "TPA3251DDV")
    assert "Extends: TPA3255DDV (sibling file in this symdir)" in control.details(row)


def test_details_warns_about_a_multi_symbol_file(lib_root):
    kf.write_multi_symbol(
        lib_root / "symbols" / "C.kicad_symdir" / "Cache.kicad_sym", ["A", "B"]
    )
    control = ct.Controller(lib_root)
    row = control.rows(ct.KIND_SYMBOL)[0]
    assert "one symbol per file" in control.details(row)


def test_details_for_a_footprint_lists_its_users(control):
    row = next(r for r in control.rows(ct.KIND_FOOTPRINT) if r.name == "SOP63P810X120-44N")
    text = control.details(row)
    assert "Amp_Test:TPA3251DDV" in text and "Amp_Test:TPA3255DDV" in text


def test_details_includes_provenance_when_present(control):
    control.prov.record("Amp_Test", pv.KIND_SYMBOL, "TPA3255DDV",
                        original_name="orig.kicad_sym", source="SnapEDA",
                        imported="2026-10-04")
    row = next(r for r in control.rows(ct.KIND_SYMBOL) if r.name == "TPA3255DDV")
    text = control.details(row)
    assert "Original name: orig.kicad_sym" in text
    assert "Imported on: 2026-10-04" in text


# --------------------------------------------------------------------------
# Category validation (drives the New-category dialog)
# --------------------------------------------------------------------------

def test_a_good_category_name_validates_silently(control):
    assert control.validate_new_category("Passives_Sagami") == (True, "")


def test_an_invalid_category_name_is_rejected_with_a_reason(control):
    ok, message = control.validate_new_category("Bad:Name")
    assert ok is False and "lib_id" in message


def test_a_case_duplicate_is_allowed_but_warned_about(control):
    ok, message = control.validate_new_category("amp_test")
    assert ok is True and "only by case" in message


def test_an_official_collision_is_allowed_but_warned_about(control):
    ok, message = control.validate_new_category("Amplifier_Audio")
    assert ok is True and "official KiCad library" in message


# --------------------------------------------------------------------------
# Staleness and provenance problems
# --------------------------------------------------------------------------

def test_tables_are_not_stale_after_generating(control):
    assert control.tables_are_stale is False


def test_adding_a_category_makes_the_tables_stale(control):
    kf.write_symbol(control.root / "symbols" / "New.kicad_symdir" / "S.kicad_sym")
    control.refresh()
    assert control.tables_are_stale is True


def test_a_corrupt_provenance_file_is_reported_not_raised(populated_lib):
    (populated_lib / "provenance.json").write_text("{broken")
    control = ct.Controller(populated_lib)
    assert "not valid JSON" in control.provenance_error
    assert control.rows(ct.KIND_SYMBOL)          # still usable


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------

def test_plan_rename_comes_back_as_a_reviewable_plan(control):
    plan = control.plan_rename(ct.KIND_SYMBOL, "Amp_Test", "TPA3255DDV", "NEW")
    assert not plan.is_empty
    assert any("will break" in w for w in plan.warnings)


def test_plan_move_comes_back_as_a_plan(control):
    plan = control.plan_move(ct.KIND_FOOTPRINT, "Amp_Test", "SOP63P810X120-44N", "Pkg")
    assert not plan.is_empty


def test_plan_rename_rejects_an_unrenameable_kind(control):
    with pytest.raises(ValueError, match="cannot rename"):
        control.plan_rename("gerber", "Amp_Test", "x", "y")


def test_plan_delete_warns_about_what_it_will_break(control):
    row = next(r for r in control.rows(ct.KIND_FOOTPRINT) if r.name == "SOP63P810X120-44N")
    plan = control.plan_delete([row])
    assert any("will be left dangling" in w for w in plan.warnings)
    assert any("Amp_Test:TPA3255DDV" in w for w in plan.warnings)


def test_plan_delete_warns_about_derived_symbols(control):
    row = next(r for r in control.rows(ct.KIND_SYMBOL) if r.name == "TPA3255DDV")
    plan = control.plan_delete([row])
    assert any("no longer load" in w for w in plan.warnings)


def test_building_a_plan_writes_nothing(control):
    before = {p: p.read_bytes() for p in control.root.rglob("*") if p.is_file()}
    control.plan_rename(ct.KIND_SYMBOL, "Amp_Test", "TPA3255DDV", "NEW")
    control.plan_delete(control.rows(ct.KIND_SYMBOL))
    control.plan_generate()
    after = {p: p.read_bytes() for p in control.root.rglob("*") if p.is_file()}
    assert before == after


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------

def test_apply_refreshes_and_regenerates_the_tables(control):
    plan = control.plan_rename(ct.KIND_SYMBOL, "Amp_Test", "TPA3255DDV", "RENAMED")
    result = control.apply(plan)
    assert result.ok
    assert control.lib.find_symbol("Amp_Test", "RENAMED") is not None
    assert control.tables_are_stale is False


def test_apply_saves_provenance(control):
    control.prov.record("Amp_Test", pv.KIND_SYMBOL, "TPA3255DDV", source="SnapEDA")
    plan = control.plan_rename(ct.KIND_SYMBOL, "Amp_Test", "TPA3255DDV", "RENAMED")
    control.apply(plan)
    payload = json.loads((control.root / "provenance.json").read_text())
    assert "Amp_Test/symbol/RENAMED" in payload["items"]


def test_apply_reports_progress(control):
    seen = []
    plan = control.plan_rename(ct.KIND_SYMBOL, "Amp_Test", "TPA3255DDV", "RENAMED")
    control.apply(plan, on_progress=seen.append)
    assert len(seen) == len(plan.operations)


def test_a_failed_apply_still_leaves_the_controller_consistent(control):
    plan = control.plan_generate()
    plan.copy(control.root / "nonexistent", control.root / "x")
    result = control.apply(plan)
    assert not result.ok
    assert control.categories() == ["Amp_Test", "Conn_Test"]


def test_a_full_import_through_the_controller(lib_root, tmp_path):
    control = ct.Controller(lib_root)
    z = kf.zip_snapeda(tmp_path / "part.zip")
    with control.prepare_import(z, "Amp_New") as preview:
        result = control.apply(preview.plan)
    assert result.ok
    assert control.lib.find_symbol("Amp_New", "TPA3255DDV") is not None
    assert control.tables_are_stale is False
    assert control.refs.dangling == []


def test_audit_is_reachable_from_the_controller(control):
    report = control.audit(use_kicad_cli=False)
    assert report.exit_code == 0


def test_package_plan_is_reachable_from_the_controller(control, tmp_path):
    project = kf.write_project(
        tmp_path / "proj",
        symbols=[("Amp_Test:TPA3255DDV", "Amp_Test:SOP63P810X120-44N")],
    )
    plan, result = control.plan_package(project)
    assert result.packaged_symbols == ["Amp_Test:TPA3255DDV"]
    assert not plan.is_empty
