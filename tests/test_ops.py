"""Phase 1: ops.py -- plan first, then apply."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core import ops
from src.core.ops import ConflictPolicy, OpKind, Operation, Plan


@pytest.fixture
def plan(tmp_path) -> Plan:
    return Plan(root=tmp_path, title="test plan")


# --------------------------------------------------------------------------
# Planning touches nothing
# --------------------------------------------------------------------------

def test_building_a_plan_does_not_touch_the_disk(tmp_path, plan):
    src = tmp_path / "src.txt"
    src.write_text("x")
    plan.copy(src, tmp_path / "out" / "dst.txt")
    plan.write(tmp_path / "out" / "gen.txt", "generated")
    plan.create_dir(tmp_path / "newdir")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["src.txt"]


def test_dry_run_apply_changes_nothing(tmp_path, plan):
    plan.write(tmp_path / "x.txt", "data")
    result = ops.apply(plan, dry_run=True)
    assert not (tmp_path / "x.txt").exists()
    assert result.ok and result.applied == [] and len(result.skipped) == 1


def test_summary_lists_operations_relative_to_the_root(tmp_path, plan):
    plan.write(tmp_path / "sub" / "x.txt", "d")
    text = plan.summary()
    assert "test plan" in text and "sub/x.txt" in text
    assert str(tmp_path) not in text


def test_empty_plan_summary_says_nothing_to_do(plan):
    assert plan.is_empty
    assert "(nothing to do)" in plan.summary()


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def test_apply_creates_parent_directories_lazily(tmp_path, plan):
    """
    No CreateDir operation is needed: a file op makes its own parents. This
    is what stops empty category directories from being created for an
    import that places no files.
    """
    plan.write(tmp_path / "a" / "b" / "c.txt", "hi")
    assert ops.apply(plan).ok
    assert (tmp_path / "a/b/c.txt").read_text() == "hi"


def test_apply_copies_moves_writes_and_deletes(tmp_path, plan):
    src = tmp_path / "src.txt"
    src.write_text("payload")
    doomed = tmp_path / "doomed.txt"
    doomed.write_text("bye")

    plan.copy(src, tmp_path / "copy.txt")
    plan.move(src, tmp_path / "moved.txt")
    plan.write(tmp_path / "written.txt", "fresh")
    plan.delete(doomed)

    assert ops.apply(plan).ok
    assert (tmp_path / "copy.txt").read_text() == "payload"
    assert (tmp_path / "moved.txt").read_text() == "payload"
    assert not src.exists()
    assert (tmp_path / "written.txt").read_text() == "fresh"
    assert not doomed.exists()


def test_write_text_operations_always_emit_lf(tmp_path, plan):
    plan.write(tmp_path / "x.kicad_mod", '(footprint "X"\n)\n')
    ops.apply(plan)
    assert b"\r" not in (tmp_path / "x.kicad_mod").read_bytes()


def test_delete_dir_if_empty_leaves_a_populated_directory_alone(tmp_path, plan):
    empty = tmp_path / "empty"
    empty.mkdir()
    full = tmp_path / "full"
    full.mkdir()
    (full / "keep.txt").write_text("k")

    plan.delete_dir_if_empty(empty)
    plan.delete_dir_if_empty(full)
    result = ops.apply(plan)
    assert result.ok
    assert not empty.exists()
    assert full.exists()
    assert len(result.applied) == 1 and len(result.skipped) == 1


def test_deleting_a_missing_file_is_a_no_op_not_a_failure(tmp_path, plan):
    plan.delete(tmp_path / "never_existed.txt")
    result = ops.apply(plan)
    assert result.ok and result.applied == [] and len(result.skipped) == 1


def test_operations_run_in_the_order_they_were_added(tmp_path, plan):
    target = tmp_path / "x.txt"
    plan.write(target, "first")
    plan.write(target, "second")
    ops.apply(plan)
    assert target.read_text() == "second"


# --------------------------------------------------------------------------
# Failure reporting
# --------------------------------------------------------------------------

def test_apply_stops_at_the_first_failure_and_reports_what_was_applied(tmp_path, plan):
    plan.write(tmp_path / "ok.txt", "done")
    plan.copy(tmp_path / "missing_source.txt", tmp_path / "never.txt")
    plan.write(tmp_path / "unreached.txt", "nope")

    result = ops.apply(plan)
    assert not result.ok
    assert (tmp_path / "ok.txt").exists()
    assert not (tmp_path / "unreached.txt").exists()
    assert len(result.applied) == 1
    assert result.failed[0].kind is OpKind.COPY_FILE
    assert "earlier operations were applied" in result.summary(tmp_path)


def test_a_failed_write_leaves_no_partial_file(tmp_path, plan):
    """A directory where a file should go: the write fails, nothing is left."""
    clash = tmp_path / "clash"
    clash.mkdir()
    plan.write(clash, "data")
    result = ops.apply(plan)
    assert not result.ok
    assert clash.is_dir()
    assert [p.name for p in clash.iterdir()] == []


# --------------------------------------------------------------------------
# Conflict policies
# --------------------------------------------------------------------------

def test_default_policy_skips_and_warns_rather_than_overwriting(tmp_path, plan):
    existing = tmp_path / "FP.kicad_mod"
    existing.write_text("original")
    final = ops.resolve_conflict(plan, existing, ConflictPolicy.SKIP, what="footprint")
    assert final is None
    assert plan.conflicts[0].resolution == "skipped"
    assert any("already exists" in w for w in plan.warnings)
    assert existing.read_text() == "original"


def test_overwrite_policy_keeps_the_same_target_but_warns(tmp_path, plan):
    existing = tmp_path / "FP.kicad_mod"
    existing.write_text("original")
    final = ops.resolve_conflict(plan, existing, ConflictPolicy.OVERWRITE)
    assert final == existing
    assert plan.conflicts[0].resolution == "overwritten"
    assert any("overwriting" in w for w in plan.warnings)


def test_rename_policy_picks_the_first_free_suffix(tmp_path, plan):
    (tmp_path / "FP.kicad_mod").write_text("a")
    (tmp_path / "FP_1.kicad_mod").write_text("b")
    final = ops.resolve_conflict(plan, tmp_path / "FP.kicad_mod", ConflictPolicy.RENAME)
    assert final.name == "FP_2.kicad_mod"
    assert plan.conflicts[0].resolution == "renamed"


def test_no_conflict_is_recorded_when_the_target_is_free(tmp_path, plan):
    target = tmp_path / "fresh.kicad_mod"
    assert ops.resolve_conflict(plan, target, ConflictPolicy.SKIP) == target
    assert plan.conflicts == [] and plan.warnings == []


def test_unique_target_returns_the_original_when_free(tmp_path):
    assert ops.unique_target(tmp_path / "free.txt") == tmp_path / "free.txt"


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------

def test_extend_folds_one_plan_into_another_preserving_order(tmp_path):
    a = Plan(root=tmp_path, title="a")
    b = Plan(root=tmp_path, title="b")
    a.write(tmp_path / "1.txt", "1")
    b.write(tmp_path / "2.txt", "2")
    b.warn("careful")
    a.extend(b)
    assert [op.target.name for op in a.operations] == ["1.txt", "2.txt"]
    assert a.warnings == ["careful"]


def test_warn_does_not_duplicate_the_same_message(plan):
    plan.warn("same"); plan.warn("same")
    assert plan.warnings == ["same"]


def test_on_progress_callback_sees_each_applied_operation(tmp_path, plan):
    plan.write(tmp_path / "a.txt", "a")
    plan.write(tmp_path / "b.txt", "b")
    seen = []
    ops.apply(plan, on_progress=seen.append)
    assert [op.target.name for op in seen] == ["a.txt", "b.txt"]
