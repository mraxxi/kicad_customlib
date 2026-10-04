"""
View-model for the GUI.

Holds every decision the interface needs to make, with no Tk in sight, so it
can be tested headlessly and so the widgets stay thin. The widgets' only job
is to render Rows and to hand user intent back here.

Mirrors the core's discipline: anything that changes the disk comes back as
an ops.Plan for the user to look at before it runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..core import check as check_mod
from ..core import ingest as ingest_mod
from ..core import library as lb
from ..core import naming
from ..core import ops
from ..core import packager as packager_mod
from ..core import provenance as pv
from ..core import refactor as rf
from ..core import table_gen as tg

KIND_SYMBOL = "symbol"
KIND_FOOTPRINT = "footprint"
KIND_MODEL = "model"
KINDS = (KIND_SYMBOL, KIND_FOOTPRINT, KIND_MODEL)

# Column headings per kind, in display order.
COLUMNS: Dict[str, Tuple[str, ...]] = {
    KIND_SYMBOL: ("Name", "Category", "Footprint", "3D", "Source"),
    KIND_FOOTPRINT: ("Name", "Category", "3D models", "Used by", "Source"),
    KIND_MODEL: ("Name", "Category", "Used by", "Size", "Source"),
}

OK = "✓"       # check mark
MISSING = "✗"  # ballot X


@dataclass(frozen=True)
class Row:
    """One line in the browser. `iid` is a stable identity, never display text."""
    iid: str
    kind: str
    category: str
    name: str
    values: Tuple[str, ...]
    path: Optional[Path] = None
    problem: str = ""

    @property
    def is_category(self) -> bool:
        return self.kind == "category"


def make_iid(kind: str, category: str, name: str) -> str:
    """
    'kind|category|name'.

    The old GUI read the selected item back out of the Treeview's displayed
    values, where Tk had already turned a numeric-looking name like '3255'
    into an int, so the lookup failed. An explicit string iid avoids the
    whole problem.
    """
    return f"{kind}|{category}|{name}"


def parse_iid(iid: str) -> Tuple[str, str, str]:
    kind, category, name = iid.split("|", 2)
    return kind, category, name


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class Controller:
    """Everything the GUI needs, independent of any toolkit."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.lib: lb.Library = lb.Library(root=self.root)
        self.refs: lb.Refs = lb.Refs()
        self.prov: pv.Provenance = pv.Provenance(path=self.root / pv.FILENAME)
        self.provenance_error: str = ""
        self.refresh()

    # -- state ------------------------------------------------------------
    def refresh(self) -> None:
        """Re-scan the disk. Cheap enough to call after every change."""
        self.lib = lb.scan(self.root)
        self.refs = self.lib.resolve()
        try:
            self.prov = pv.load(self.root)
            self.provenance_error = ""
        except pv.ProvenanceError as exc:
            self.prov = pv.Provenance(path=self.root / pv.FILENAME)
            self.provenance_error = str(exc)

    @property
    def tables_are_stale(self) -> bool:
        return tg.is_stale(self.root, self.lib)

    def categories(self) -> List[str]:
        return self.lib.category_names()

    def category_label(self, name: str) -> str:
        cat = self.lib.categories[name]
        if cat.is_empty:
            return f"{name}  (empty)"
        bits = []
        if cat.symbol_count:
            bits.append(f"{cat.symbol_count} sym")
        if cat.footprint_count:
            bits.append(f"{cat.footprint_count} fp")
        if cat.model_count:
            bits.append(f"{cat.model_count} 3d")
        return f"{name}  ({', '.join(bits)})"

    def counts_summary(self) -> str:
        return (
            f"{len(self.lib.symbols)} symbols, {len(self.lib.footprints)} footprints, "
            f"{len(self.lib.models)} 3D models in {len(self.lib.categories)} categories"
        )

    # -- rows -------------------------------------------------------------
    def _source_of(self, category: str, kind: str, name: str) -> str:
        item = self.prov.get(category, kind, name)
        if item is None:
            return ""
        return item.source or item.original_name or ""

    def _problem_for(self, lib_id: str) -> str:
        for d in self.refs.dangling:
            if d.source == lib_id:
                return d.detail or d.kind
        return ""

    def rows(
        self, kind: str, *, category: Optional[str] = None, search: str = ""
    ) -> List[Row]:
        """
        The rows to display, already filtered and ordered.

        Filtering lives here rather than in the widget so the behaviour is
        testable without a display.
        """
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        needle = search.strip().lower()
        out: List[Row] = []

        if kind == KIND_SYMBOL:
            for sym in self.lib.symbols:
                if category and sym.category != category:
                    continue
                fp_ref = (sym.footprint_ref or "").strip()
                has_models = bool(
                    self.refs.footprint_to_models.get(fp_ref) if fp_ref else None
                )
                problem = sym.error or self._problem_for(sym.lib_id)
                out.append(Row(
                    iid=make_iid(kind, sym.category, sym.name),
                    kind=kind, category=sym.category, name=sym.name,
                    values=(
                        sym.name, sym.category,
                        fp_ref or "(none)",
                        OK if has_models else MISSING,
                        self._source_of(sym.category, kind, sym.name),
                    ),
                    path=sym.path, problem=problem,
                ))

        elif kind == KIND_FOOTPRINT:
            for fp in self.lib.footprints:
                if category and fp.category != category:
                    continue
                users = self.refs.footprint_users.get(fp.lib_id, [])
                problem = fp.error or self._problem_for(fp.lib_id)
                out.append(Row(
                    iid=make_iid(kind, fp.category, fp.name),
                    kind=kind, category=fp.category, name=fp.name,
                    values=(
                        fp.name, fp.category,
                        str(len(fp.model_paths)) if fp.model_paths else MISSING,
                        str(len(users)),
                        self._source_of(fp.category, kind, fp.name),
                    ),
                    path=fp.path, problem=problem,
                ))

        else:
            for model in self.lib.models:
                if category and model.category != category:
                    continue
                users = self.refs.model_users.get(self.lib.rel(model.path), [])
                try:
                    size = _human_size(model.path.stat().st_size)
                except OSError:
                    size = "?"
                out.append(Row(
                    iid=make_iid(kind, model.category, model.filename),
                    kind=kind, category=model.category, name=model.filename,
                    values=(
                        model.filename, model.category, str(len(users)), size,
                        self._source_of(model.category, kind, model.filename),
                    ),
                    path=model.path,
                    problem="" if users else "not referenced by any footprint",
                ))

        if needle:
            out = [r for r in out if any(needle in str(v).lower() for v in r.values)]
        return out

    def details(self, row: Row) -> str:
        """The text for the details pane."""
        lines = [f"{row.kind.title()}: {row.name}", f"Category: {row.category}"]
        if row.path:
            lines.append(f"File: {self.lib.rel(row.path)}")

        if row.kind == KIND_SYMBOL:
            sym = self.lib.find_symbol(row.category, row.name)
            if sym:
                lines.append(f"Internal name: {sym.internal_name or '(none)'}")
                lines.append(f"Footprint: {sym.footprint_ref or '(not set)'}")
                if sym.extends:
                    lines.append(f"Extends: {sym.extends} (sibling file in this symdir)")
                if sym.extra_symbols:
                    lines.append(
                        f"WARNING: this file also declares "
                        f"{', '.join(sym.extra_symbols)}; KiCad libraries hold one "
                        f"symbol per file"
                    )
        elif row.kind == KIND_FOOTPRINT:
            fp = self.lib.find_footprint(row.category, row.name)
            if fp:
                lines.append(f"Internal name: {fp.internal_name or '(none)'}")
                for raw in fp.model_paths or ():
                    lines.append(f"3D model: {raw}")
                if not fp.model_paths:
                    lines.append("3D model: (none)")
                users = self.refs.footprint_users.get(fp.lib_id, [])
                lines.append(f"Used by: {', '.join(users) if users else '(nothing)'}")
        else:
            users = self.refs.model_users.get(
                self.lib.rel(row.path) if row.path else "", []
            )
            lines.append(f"Used by: {', '.join(users) if users else '(nothing)'}")

        item = self.prov.get(row.category, row.kind, row.name)
        if item:
            if item.original_name:
                lines.append(f"Original name: {item.original_name}")
            if item.source:
                lines.append(f"Imported from: {item.source}")
            if item.imported:
                lines.append(f"Imported on: {item.imported}")

        if row.problem:
            lines.append(f"PROBLEM: {row.problem}")
        return "\n".join(lines)

    # -- category validation ----------------------------------------------
    def validate_new_category(self, name: str) -> Tuple[bool, str]:
        """(ok, message) for the New-category dialog's live feedback."""
        try:
            warnings = naming.check_category(name, existing=self.categories())
        except naming.NameError_ as exc:
            return False, str(exc)
        return True, warnings[0] if warnings else ""

    # -- plans -------------------------------------------------------------
    def plan_generate(self) -> ops.Plan:
        return tg.plan_generate(self.root, self.lib)

    def prepare_import(
        self,
        source: Path,
        category: str,
        *,
        conflict: ops.ConflictPolicy = ops.ConflictPolicy.SKIP,
    ) -> ingest_mod.IngestPreview:
        return ingest_mod.prepare(self.root, source, category, conflict=conflict)

    def plan_rename(
        self,
        kind: str,
        category: str,
        old: str,
        new: str,
        *,
        rename_model: bool = False,
        conflict: ops.ConflictPolicy = ops.ConflictPolicy.SKIP,
    ) -> ops.Plan:
        common = dict(conflict=conflict, lib=self.lib)
        if kind == rf.KIND_CATEGORY:
            return rf.plan_rename_category(self.root, old, new, **common)
        if kind == KIND_SYMBOL:
            return rf.plan_rename_symbol(self.root, category, old, new, **common)
        if kind == KIND_FOOTPRINT:
            return rf.plan_rename_footprint(
                self.root, category, old, new, rename_model=rename_model, **common
            )
        if kind == KIND_MODEL:
            return rf.plan_rename_model(self.root, category, old, new, **common)
        raise ValueError(f"cannot rename a {kind!r}")

    def plan_move(
        self,
        kind: str,
        category: str,
        name: str,
        new_category: str,
        *,
        conflict: ops.ConflictPolicy = ops.ConflictPolicy.SKIP,
    ) -> ops.Plan:
        return rf.plan_move(
            self.root, kind, category, name, new_category,
            conflict=conflict, lib=self.lib,
        )

    def plan_delete(self, rows: Sequence[Row]) -> ops.Plan:
        """
        Delete the selected items.

        References to them are deliberately *not* rewritten -- there is no
        sensible replacement -- so the plan says what will break instead.
        """
        plan = ops.Plan(root=self.root, title=f"Delete {len(rows)} item(s)")
        for row in rows:
            if row.path is None:
                continue
            plan.delete(row.path, note=f"{row.kind} '{row.name}'")
            if row.kind == KIND_FOOTPRINT:
                users = self.refs.footprint_users.get(f"{row.category}:{row.name}", [])
                if users:
                    plan.warn(
                        f"{len(users)} symbol(s) reference '{row.category}:{row.name}' "
                        f"and will be left dangling: {', '.join(users)}"
                    )
            elif row.kind == KIND_MODEL:
                users = self.refs.model_users.get(self.lib.rel(row.path), [])
                if users:
                    plan.warn(
                        f"{len(users)} footprint(s) point at this model and will be "
                        f"left dangling: {', '.join(users)}"
                    )
            elif row.kind == KIND_SYMBOL:
                derived = [
                    s.lib_id for s in self.lib.symbols_in(row.category)
                    if s.extends == row.name
                ]
                if derived:
                    plan.warn(
                        f"{len(derived)} derived symbol(s) extend '{row.name}' and "
                        f"will no longer load: {', '.join(derived)}"
                    )
        for category in {r.category for r in rows}:
            for sub, suffix in (
                ("symbols", lb.SYMDIR_SUFFIX),
                ("footprints", lb.PRETTY_SUFFIX),
                ("3dmodels", lb.SHAPES_SUFFIX),
            ):
                plan.delete_dir_if_empty(self.root / sub / f"{category}{suffix}")
        return plan

    def plan_package(
        self, project_dir: Path, out_dir: Optional[Path] = None
    ) -> Tuple[ops.Plan, packager_mod.PackageResult]:
        return packager_mod.plan_package(self.root, project_dir, out_dir, lib=self.lib)

    def audit(self, *, use_kicad_cli: bool = True) -> check_mod.Report:
        return check_mod.run(self.root, use_kicad_cli=use_kicad_cli, lib=self.lib)

    # -- applying ----------------------------------------------------------
    def apply(
        self,
        plan: ops.Plan,
        *,
        on_progress: Optional[Callable[[ops.Operation], None]] = None,
    ) -> ops.Result:
        """
        Apply a plan, then put the library back in a consistent state:
        deferred provenance edits run, provenance saved, tables regenerated,
        index re-scanned.
        """
        result = ops.apply(plan, on_progress=on_progress)
        if not result.ok:
            self.refresh()
            return result

        # Provenance is edited here, once, and only now that the operations
        # have actually run. Doing it while the plan was built meant the GUI's
        # per-keystroke preview rewrote the keys on every character typed.
        changed = pv.apply_edits(self.prov, plan.provenance)
        if changed or self.prov.items or self.prov.existed:
            self.prov.save()

        self.refresh()
        table_plan = self.plan_generate()
        if not table_plan.is_empty:
            ops.apply(table_plan)
            self.refresh()
        return result
