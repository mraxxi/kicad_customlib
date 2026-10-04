"""
Plan / apply engine.

Every mutating action in this library is a two-step affair: build a Plan,
which is pure computation that never touches the disk, then apply() it. The
GUI shows the plan as a preview, the CLI prints it for --dry-run, and both
run exactly the operations the user was shown.

Two rules worth stating outright:

* Nothing is ever silently overwritten. The default conflict policy is SKIP
  with a warning; OVERWRITE and RENAME have to be asked for.
* Directories are created lazily, only when a file is actually placed in
  them. Eagerly creating the three mirror directories is what left this repo
  with empty '3255' and 'TI-TPAxxx_AUDIO-AMP' categories advertised in the
  generated tables -- and git does not track empty directories, so the other
  machine got tables pointing at folders that did not exist.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from . import s_expr as sx


class ConflictPolicy(str, Enum):
    SKIP = "skip"
    OVERWRITE = "overwrite"
    RENAME = "rename"


class OpKind(str, Enum):
    CREATE_DIR = "CreateDir"
    COPY_FILE = "CopyFile"
    MOVE_FILE = "MoveFile"
    WRITE_TEXT = "WriteText"
    DELETE_FILE = "DeleteFile"
    DELETE_DIR_IF_EMPTY = "DeleteDirIfEmpty"


class ApplyError(RuntimeError):
    """An operation failed. The Result says what had already been applied."""


@dataclass
class Operation:
    """One filesystem change. Paths are absolute."""
    kind: OpKind
    target: Path
    source: Optional[Path] = None
    text: Optional[str] = None
    note: str = ""

    def describe(self, root: Optional[Path] = None) -> str:
        def rel(p: Optional[Path]) -> str:
            if p is None:
                return ""
            if root is None:
                return str(p)
            try:
                return p.resolve().relative_to(root).as_posix()
            except ValueError:
                return str(p)

        if self.kind in (OpKind.COPY_FILE, OpKind.MOVE_FILE):
            arrow = "->" if self.kind is OpKind.MOVE_FILE else "=>"
            body = f"{rel(self.source)} {arrow} {rel(self.target)}"
        else:
            body = rel(self.target)
        out = f"{self.kind.value:<18} {body}"
        return f"{out}  # {self.note}" if self.note else out


@dataclass
class Conflict:
    """A target that already exists, and what the policy decided to do."""
    target: Path
    policy: ConflictPolicy
    resolution: str              # skipped | overwritten | renamed
    final_target: Optional[Path] = None


@dataclass
class Plan:
    """An ordered, reviewable set of operations. Building one touches nothing."""
    root: Path
    title: str = ""
    operations: List[Operation] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    # -- construction -----------------------------------------------------
    def add(self, op: Operation) -> Operation:
        self.operations.append(op)
        return op

    def create_dir(self, target: Path, note: str = "") -> Operation:
        return self.add(Operation(OpKind.CREATE_DIR, Path(target), note=note))

    def copy(self, source: Path, target: Path, note: str = "") -> Operation:
        return self.add(Operation(OpKind.COPY_FILE, Path(target), source=Path(source), note=note))

    def move(self, source: Path, target: Path, note: str = "") -> Operation:
        return self.add(Operation(OpKind.MOVE_FILE, Path(target), source=Path(source), note=note))

    def write(self, target: Path, text: str, note: str = "") -> Operation:
        return self.add(Operation(OpKind.WRITE_TEXT, Path(target), text=text, note=note))

    def delete(self, target: Path, note: str = "") -> Operation:
        return self.add(Operation(OpKind.DELETE_FILE, Path(target), note=note))

    def delete_dir_if_empty(self, target: Path, note: str = "") -> Operation:
        return self.add(Operation(OpKind.DELETE_DIR_IF_EMPTY, Path(target), note=note))

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)

    def extend(self, other: "Plan") -> None:
        """Fold another plan into this one, keeping order."""
        self.operations.extend(other.operations)
        for w in other.warnings:
            self.warn(w)
        self.conflicts.extend(other.conflicts)
        self.notes.extend(other.notes)

    # -- inspection -------------------------------------------------------
    @property
    def is_empty(self) -> bool:
        return not self.operations

    def targets(self) -> List[Path]:
        return [op.target for op in self.operations]

    def summary(self) -> str:
        """Human-readable preview. This is what --dry-run prints."""
        lines: List[str] = []
        if self.title:
            lines.append(self.title)
        if self.is_empty:
            lines.append("  (nothing to do)")
        for op in self.operations:
            lines.append(f"  {op.describe(self.root)}")
        for c in self.conflicts:
            lines.append(
                f"  ! exists: {_rel(c.target, self.root)} -> {c.resolution}"
                + (f" as {_rel(c.final_target, self.root)}" if c.resolution == "renamed" and c.final_target else "")
            )
        for w in self.warnings:
            lines.append(f"  warning: {w}")
        for n in self.notes:
            lines.append(f"  note: {n}")
        return "\n".join(lines)


def _rel(p: Optional[Path], root: Path) -> str:
    if p is None:
        return ""
    try:
        return p.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(p)


@dataclass
class Result:
    """What apply() actually did."""
    applied: List[Operation] = field(default_factory=list)
    skipped: List[Operation] = field(default_factory=list)
    failed: Optional[Tuple[Operation, str]] = None

    @property
    def ok(self) -> bool:
        return self.failed is None

    def summary(self, root: Optional[Path] = None) -> str:
        lines = [f"applied {len(self.applied)} operation(s)"]
        if self.skipped:
            lines.append(f"skipped {len(self.skipped)}")
        if self.failed:
            op, err = self.failed
            lines.append(f"FAILED on {op.describe(root)}: {err}")
            lines.append("earlier operations were applied and were NOT rolled back")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Conflict handling (plan time -- the disk is read, never written)
# --------------------------------------------------------------------------

def unique_target(target: Path) -> Path:
    """`FP.kicad_mod` -> `FP_1.kicad_mod` -> `FP_2.kicad_mod`, first one free."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(1, 1000):
        candidate = target.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise ApplyError(f"cannot find a free name near {target}")


def resolve_conflict(
    plan: Plan, target: Path, policy: ConflictPolicy, *, what: str = "file"
) -> Optional[Path]:
    """
    Decide where a file should actually land.

    Returns the final target, or None when the item should be dropped.
    Records the decision on the plan so the preview shows it.
    """
    if not target.exists():
        return target

    rel = _rel(target, plan.root)
    if policy is ConflictPolicy.OVERWRITE:
        plan.conflicts.append(Conflict(target, policy, "overwritten", target))
        plan.warn(f"overwriting existing {what} {rel}")
        return target

    if policy is ConflictPolicy.RENAME:
        final = unique_target(target)
        plan.conflicts.append(Conflict(target, policy, "renamed", final))
        plan.warn(f"{what} {rel} exists; importing as {_rel(final, plan.root)} instead")
        return final

    plan.conflicts.append(Conflict(target, policy, "skipped", None))
    plan.warn(f"{what} {rel} already exists; skipped (use --conflict overwrite or rename)")
    return None


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def _execute(op: Operation) -> bool:
    """Run one operation. Returns False when it was a no-op."""
    if op.kind is OpKind.CREATE_DIR:
        op.target.mkdir(parents=True, exist_ok=True)
        return True

    if op.kind is OpKind.COPY_FILE:
        assert op.source is not None
        op.target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(op.source, op.target)
        return True

    if op.kind is OpKind.MOVE_FILE:
        assert op.source is not None
        op.target.parent.mkdir(parents=True, exist_ok=True)
        # shutil.move handles a cross-filesystem move, which os.replace cannot.
        shutil.move(str(op.source), str(op.target))
        return True

    if op.kind is OpKind.WRITE_TEXT:
        assert op.text is not None
        sx.write_text(op.target, op.text)
        return True

    if op.kind is OpKind.DELETE_FILE:
        if op.target.exists():
            op.target.unlink()
            return True
        return False

    if op.kind is OpKind.DELETE_DIR_IF_EMPTY:
        if op.target.is_dir() and not any(op.target.iterdir()):
            op.target.rmdir()
            return True
        return False

    raise ApplyError(f"unknown operation kind {op.kind!r}")


def apply(
    plan: Plan,
    *,
    dry_run: bool = False,
    on_progress: Optional[Callable[[Operation], None]] = None,
) -> Result:
    """
    Execute a plan in order.

    On the first failure execution stops and the Result names the operation
    that failed plus everything already applied. There is no automatic
    rollback -- but because every file write goes through the atomic
    write_text, no individual file is ever left half-written.
    """
    result = Result()
    if dry_run:
        result.skipped = list(plan.operations)
        return result

    for op in plan.operations:
        try:
            changed = _execute(op)
        except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
            result.failed = (op, f"{type(exc).__name__}: {exc}")
            return result
        (result.applied if changed else result.skipped).append(op)
        if on_progress is not None:
            on_progress(op)

    return result
