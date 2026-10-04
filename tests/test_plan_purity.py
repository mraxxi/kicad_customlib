"""
Phase 0: building a plan must not change anything.

`tests/test_refactor.py::test_planning_a_refactor_writes_nothing` compared
file bytes on disk and never passed a Provenance object, so it could not see
that plan building mutated provenance in memory. The GUI rebuilds the plan on
every keystroke to keep its live preview current, which walked a provenance
key through every partially-typed name and stranded it on the first one.

These tests close that gap: they assert the Provenance object itself is
untouched by planning, and that the deferred edits still land on apply.
"""

from __future__ import annotations

import pytest

from src.core import ingest
from src.core import library as lb
from src.core import ops
from src.core import provenance as pv
from src.core import refactor as rf
from tests import kicad_fixtures as kf


@pytest.fixture
def prov_with_history(populated_lib):
    """Provenance covering everything in the populated library."""
    prov = pv.load(populated_lib)
    prov.record("Amp_Test", pv.KIND_SYMBOL, "TPA3255DDV",
                original_name="vendor_tpa.kicad_sym", source="SnapEDA zip",
                imported="2026-07-09")
    prov.record("Amp_Test", pv.KIND_FOOTPRINT, "SOP63P810X120-44N",
                original_name="vendor_fp.kicad_mod", source="SnapEDA zip")
    prov.record("Amp_Test", pv.KIND_MODEL, "TPA3255DDV.step",
                original_name="vendor.step", source="SnapEDA zip")
    return prov


# --------------------------------------------------------------------------
# Planning is pure
# --------------------------------------------------------------------------

def test_planning_a_rename_does_not_touch_provenance(populated_lib, prov_with_history):
    before = prov_with_history.to_json_text()
    rf.plan_rename_symbol(populated_lib, "Amp_Test", "TPA3255DDV", "NEW")
    rf.plan_rename_footprint(populated_lib, "Amp_Test", "SOP63P810X120-44N", "NEW_FP")
    rf.plan_rename_model(populated_lib, "Amp_Test", "TPA3255DDV.step", "new.step")
    rf.plan_rename_category(populated_lib, "Amp_Test", "TI_Amps")
    rf.plan_move(populated_lib, rf.KIND_SYMBOL, "Conn_Test", "KF2EDG", "Conn_New")
    assert prov_with_history.to_json_text() == before


def test_planning_an_import_does_not_touch_provenance(lib_root, tmp_path):
    prov = pv.load(lib_root)
    prov.record("Existing", pv.KIND_SYMBOL, "Thing", source="earlier")
    before = prov.to_json_text()
    z = kf.zip_snapeda(tmp_path / "part.zip")
    with ingest.prepare(lib_root, z, "Amp_New") as preview:
        assert not preview.plan.is_empty
    assert prov.to_json_text() == before


def test_previewing_an_import_then_cancelling_records_nothing(lib_root, tmp_path):
    """
    `ImportDialog._grouped_plan()` is shared by Preview and Import, so a
    preview that is never applied must leave provenance alone.
    """
    prov = pv.load(lib_root)
    z = kf.zip_snapeda(tmp_path / "part.zip")
    with ingest.prepare(lib_root, z, "Amp_New") as preview:
        pass                                    # previewed, then cancelled
    assert prov.items == {}


def test_rebuilding_a_plan_per_keystroke_is_harmless(populated_lib, prov_with_history):
    """
    The exact reproduction. Typing "TI_Amps" rebuilt the plan seven times; the
    first rebuild moved the key to "T/symbol/TPA3255DDV" and the rest found
    nothing left to move.
    """
    target = "TI_Amps"
    plan = None
    for i in range(1, len(target) + 1):
        plan = rf.plan_rename_category(populated_lib, "Amp_Test", target[:i])

    # Nothing happened while typing...
    assert prov_with_history.get("Amp_Test", pv.KIND_SYMBOL, "TPA3255DDV") is not None
    assert prov_with_history.get("T", pv.KIND_SYMBOL, "TPA3255DDV") is None

    # ...and applying the final plan moves it exactly once, intact.
    assert ops.apply(plan).ok
    pv.apply_edits(prov_with_history, plan.provenance)
    moved = prov_with_history.get("TI_Amps", pv.KIND_SYMBOL, "TPA3255DDV")
    assert moved is not None
    assert moved.original_name == "vendor_tpa.kicad_sym"
    assert moved.source == "SnapEDA zip"
    assert moved.imported == "2026-07-09"
    assert prov_with_history.keys_for_category("Amp_Test") == []


# --------------------------------------------------------------------------
# The deferred edits still do their job
# --------------------------------------------------------------------------

def test_applying_a_rename_moves_the_provenance_key(populated_lib, prov_with_history):
    """Guards against "fixing" the bug by simply deleting the updates."""
    plan = rf.plan_rename_symbol(populated_lib, "Amp_Test", "TPA3255DDV", "RENAMED")
    assert plan.provenance, "the plan must carry the deferred edit"
    assert ops.apply(plan).ok
    assert pv.apply_edits(prov_with_history, plan.provenance) == 1
    assert prov_with_history.get("Amp_Test", pv.KIND_SYMBOL, "TPA3255DDV") is None
    assert prov_with_history.get("Amp_Test", pv.KIND_SYMBOL, "RENAMED").source == "SnapEDA zip"


def test_applying_an_import_records_provenance(lib_root, tmp_path):
    prov = pv.load(lib_root)
    z = kf.zip_snapeda(tmp_path / "vendor_bundle.zip")
    with ingest.prepare(lib_root, z, "Amp_New") as preview:
        assert ops.apply(preview.plan).ok
        pv.apply_edits(prov, preview.plan.provenance)
    item = prov.get("Amp_New", pv.KIND_SYMBOL, "TPA3255DDV")
    assert item is not None and item.source == "vendor_bundle.zip"


def test_extend_carries_deferred_edits(lib_root):
    a = ops.Plan(root=lib_root)
    b = ops.Plan(root=lib_root)
    b.record_provenance("C", pv.KIND_SYMBOL, "X", source="s")
    a.extend(b)
    assert len(a.provenance) == 1


def test_apply_edits_rejects_an_unknown_action(lib_root):
    prov = pv.load(lib_root)
    with pytest.raises(ValueError, match="unknown provenance edit action"):
        pv.apply_edits(prov, [ops.ProvenanceEdit("nonsense")])


def test_the_plan_functions_no_longer_accept_a_prov_argument(populated_lib):
    """Keeping the old mistake unrepresentable rather than merely unused."""
    with pytest.raises(TypeError):
        rf.plan_rename_symbol(
            populated_lib, "Amp_Test", "TPA3255DDV", "X", prov=object()
        )


# --------------------------------------------------------------------------
# Pruning
# --------------------------------------------------------------------------

def test_stale_keys_finds_entries_with_nothing_on_disk(populated_lib, prov_with_history):
    prov_with_history.record("Amp_Test", pv.KIND_SYMBOL, "DELETED")
    prov_with_history.record("Gone_Cat", pv.KIND_FOOTPRINT, "ALSO_GONE")
    stale = pv.stale_keys(prov_with_history, lb.scan(populated_lib))
    assert stale == ["Amp_Test/symbol/DELETED", "Gone_Cat/footprint/ALSO_GONE"]


def test_prune_removes_only_the_stale_entries(populated_lib, prov_with_history):
    prov_with_history.record("TPAxxxx_TI-", pv.KIND_SYMBOL, "TPA3255DDV")
    removed = pv.prune(prov_with_history, lb.scan(populated_lib))
    assert removed == ["TPAxxxx_TI-/symbol/TPA3255DDV"]
    assert prov_with_history.get("Amp_Test", pv.KIND_SYMBOL, "TPA3255DDV") is not None


def test_prune_is_a_no_op_on_a_clean_library(populated_lib, prov_with_history):
    assert pv.prune(prov_with_history, lb.scan(populated_lib)) == []


# --------------------------------------------------------------------------
# Recovery
# --------------------------------------------------------------------------

def test_recoverable_rehomes_an_entry_whose_category_name_is_stale(
    populated_lib, prov_with_history
):
    """
    The shape the bug left behind: the right item under a partially-typed
    category. The entry still holds the vendor filename, so moving it beats
    dropping it.
    """
    prov_with_history.items["Amp_T/symbol/TPA3251DDV"] = pv.Item(
        original_name="vendor_3251.kicad_sym", source="SnapEDA", imported="2026-07-09"
    )
    moves = pv.recoverable(prov_with_history, lb.scan(populated_lib))
    assert moves == {"Amp_T/symbol/TPA3251DDV": "Amp_Test/symbol/TPA3251DDV"}


def test_recover_moves_the_entry_and_keeps_its_details(populated_lib, prov_with_history):
    prov_with_history.items["Amp_T/symbol/TPA3251DDV"] = pv.Item(
        original_name="vendor_3251.kicad_sym", source="SnapEDA", imported="2026-07-09"
    )
    assert len(pv.recover(prov_with_history, lb.scan(populated_lib))) == 1
    moved = prov_with_history.get("Amp_Test", pv.KIND_SYMBOL, "TPA3251DDV")
    assert moved.original_name == "vendor_3251.kicad_sym"
    assert moved.imported == "2026-07-09"
    assert "Amp_T/symbol/TPA3251DDV" not in prov_with_history.items


def test_recovery_refuses_when_the_name_exists_in_two_categories(lib_root):
    """Guessing could attach the wrong vendor history to a part."""
    for cat in ("A", "B"):
        kf.write_symbol(lib_root / "symbols" / f"{cat}.kicad_symdir" / "SHARED.kicad_sym")
    prov = pv.load(lib_root)
    prov.items["Old/symbol/SHARED"] = pv.Item(original_name="x.kicad_sym")
    assert pv.recoverable(prov, lb.scan(lib_root)) == {}


def test_recovery_refuses_when_the_destination_already_has_an_entry(
    populated_lib, prov_with_history
):
    """A duplicate left by the bug carries no new information."""
    prov_with_history.items["Amp_T/symbol/TPA3255DDV"] = pv.Item(original_name="dup")
    assert pv.recoverable(prov_with_history, lb.scan(populated_lib)) == {}


def test_recovery_does_not_touch_a_genuinely_renamed_item(populated_lib, prov_with_history):
    """When the item's own name changed too, there is nothing to match on."""
    prov_with_history.items["Amp_Test/symbol/OLD_NAME_ENTIRELY"] = pv.Item(
        original_name="vendor.kicad_sym"
    )
    moves = pv.recoverable(prov_with_history, lb.scan(populated_lib))
    assert "Amp_Test/symbol/OLD_NAME_ENTIRELY" not in moves


def test_prune_after_recover_leaves_only_the_unplaceable(populated_lib, prov_with_history):
    prov_with_history.items["Amp_T/symbol/TPA3251DDV"] = pv.Item(original_name="a")
    prov_with_history.items["Amp_Test/symbol/VANISHED"] = pv.Item(original_name="b")
    lib = lb.scan(populated_lib)
    pv.recover(prov_with_history, lib)
    assert pv.prune(prov_with_history, lib) == ["Amp_Test/symbol/VANISHED"]
    assert prov_with_history.get("Amp_Test", pv.KIND_SYMBOL, "TPA3251DDV") is not None
