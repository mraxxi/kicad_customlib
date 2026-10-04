"""
Lean per-project export.

Copies only the custom components a project actually uses into a
self-contained bundle, so the project can be published without dragging the
whole multi-gigabyte library along.

Three things the previous version got wrong:

* It located 3D models by globbing `<footprint_name>*.step`, while ingest
  named the STEP after the *symbol*. When those names differed -- which was
  the normal case -- no model was packaged at all, silently. Models are now
  resolved by reading the footprint's own `(model ...)` path, which is the
  only authoritative link.
* It wrote sym-lib-table and fp-lib-table *inside* the output folder, where
  KiCad never looks: project tables live at the project root. It also
  hard-coded `${KIPRJMOD}/project_libs/`, so any `--out` elsewhere produced
  paths that pointed at nothing.
* It overwrote existing project tables wholesale, discarding any library the
  project had already configured.

Items that are not in the custom library -- official KiCad parts, most of any
real project -- are reported separately rather than being silently absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import library as lb
from . import naming
from . import ops
from . import s_expr as sx
from . import table_gen as tg

PRJ_VAR = "${KIPRJMOD}"

# (lib_id "Lib:Name") in a schematic, and the Footprint property on a symbol.
_RE_LIB_ID = re.compile(r'\(lib_id\s+"([^"]+:[^"]+)"\s*\)')
_RE_SCH_FOOTPRINT = re.compile(r'\(property\s+"Footprint"\s+"([^"]+:[^"]+)"')
# (footprint "Lib:Name" ...) in a board.
_RE_PCB_FOOTPRINT = re.compile(r'\(footprint\s+"([^"]+:[^"]+)"')


class PackageError(ValueError):
    """The export cannot be planned."""


@dataclass
class PackageResult:
    """What the export will contain, and what it could not provide."""
    project_dir: Path
    out_dir: Path
    rel_out: str
    packaged_symbols: List[str] = field(default_factory=list)
    packaged_footprints: List[str] = field(default_factory=list)
    packaged_models: List[str] = field(default_factory=list)
    unresolved_symbols: List[str] = field(default_factory=list)
    unresolved_footprints: List[str] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)
    tables_updated: List[str] = field(default_factory=list)

    def summary(self) -> str:
        addressed_as = f"{PRJ_VAR}/{self.rel_out}" if self.rel_out else PRJ_VAR
        lines = [
            f"Project:    {self.project_dir}",
            f"Bundle:     {self.out_dir}  (addressed as {addressed_as})",
            f"Symbols:    {len(self.packaged_symbols)}",
            f"Footprints: {len(self.packaged_footprints)}",
            f"3D models:  {len(self.packaged_models)}",
            f"Libraries:  {', '.join(self.categories) or '(none)'}",
        ]
        if self.unresolved_symbols or self.unresolved_footprints:
            lines.append(
                f"Not in the custom library (left for KiCad's own libraries to "
                f"provide): {len(self.unresolved_symbols)} symbol(s), "
                f"{len(self.unresolved_footprints)} footprint(s)"
            )
            for item in self.unresolved_symbols[:10]:
                lines.append(f"  symbol    {item}")
            for item in self.unresolved_footprints[:10]:
                lines.append(f"  footprint {item}")
            remaining = (len(self.unresolved_symbols) + len(self.unresolved_footprints)) - 20
            if remaining > 0:
                lines.append(f"  ... and {remaining} more")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Scanning a project
# --------------------------------------------------------------------------

def find_used_libraries(project_dir: Path) -> Tuple[Set[str], Set[str]]:
    """
    Every `Lib:Name` the project references, as (symbols, footprints).

    Both schematics and the board are scanned: a footprint can be named in
    either, and a board that has been edited directly may reference one the
    schematic does not.
    """
    project_dir = Path(project_dir).resolve()
    if not project_dir.is_dir():
        raise PackageError(f"not a directory: {project_dir}")

    symbols: Set[str] = set()
    footprints: Set[str] = set()

    for sch in sorted(project_dir.rglob("*.kicad_sch")):
        try:
            text = sx.read_text(sch)
        except OSError:
            continue
        symbols.update(_RE_LIB_ID.findall(text))
        footprints.update(_RE_SCH_FOOTPRINT.findall(text))

    for pcb in sorted(project_dir.rglob("*.kicad_pcb")):
        try:
            text = sx.read_text(pcb)
        except OSError:
            continue
        footprints.update(_RE_PCB_FOOTPRINT.findall(text))

    return symbols, footprints


# --------------------------------------------------------------------------
# Merge-safe project tables
# --------------------------------------------------------------------------

def existing_lib_names(text: str) -> List[str]:
    """Nicknames already listed in a project table."""
    try:
        r_open, _r_close = sx.root_node(text)
    except sx.SExprError:
        return []
    names: List[str] = []
    for head, open_idx, _close in sx.children(text, r_open):
        if head != "lib":
            continue
        for sub_head, sub_open, _sub_close in sx.children(text, open_idx):
            if sub_head == "name":
                toks = sx.string_tokens(text, sub_open, limit=1)
                if toks:
                    names.append(toks[0][0])
                break
    return names


def merge_table(
    existing: Optional[str], root_tag: str, entries: Sequence[tg.LibEntry]
) -> Tuple[str, List[str]]:
    """
    Add missing `(lib ...)` rows to a project table, leaving the rest intact.

    A project table usually already lists libraries the owner set up by hand.
    Replacing it wholesale, as the previous version did, silently removed
    them. Returns the new text and the nicknames that were added.
    """
    if existing is None or not existing.strip():
        text = f"({root_tag}\n\t(version {tg.TABLE_VERSION})\n"
        text += "".join(e.render() + "\n" for e in entries)
        return text + ")\n", [e.name for e in entries]

    present = set(existing_lib_names(existing))
    missing = [e for e in entries if e.name not in present]
    if not missing:
        return existing, []

    try:
        _r_open, r_close = sx.root_node(existing)
    except sx.SExprError as exc:
        raise PackageError(
            f"existing project table is not valid S-expression ({exc}); "
            f"fix or move it aside before packaging"
        ) from exc

    insert_at = r_close
    while insert_at > 0 and existing[insert_at - 1] in " \t":
        insert_at -= 1
    block = "".join(e.render() + "\n" for e in missing)
    if insert_at > 0 and existing[insert_at - 1] != "\n":
        block = "\n" + block
    return sx.apply_edits(existing, [(insert_at, insert_at, block)]), [e.name for e in missing]


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def _relative_out(project_dir: Path, out_dir: Path) -> str:
    """
    The bundle's path relative to the project, for ${KIPRJMOD}.

    A bundle outside the project cannot be addressed with ${KIPRJMOD} at all,
    which is the whole point of the export, so that is refused rather than
    quietly producing paths that resolve to nothing.
    """
    try:
        rel = out_dir.relative_to(project_dir)
    except ValueError:
        raise PackageError(
            f"the output directory must be inside the project so it can be "
            f"referenced with {PRJ_VAR}.\n  project: {project_dir}\n  output:  {out_dir}"
        ) from None
    return rel.as_posix().strip("./")


def plan_package(
    lib_root: Path,
    project_dir: Path,
    out_dir: Optional[Path] = None,
    *,
    lib: Optional[lb.Library] = None,
) -> Tuple[ops.Plan, PackageResult]:
    """Build the export plan and a report of what it will and will not contain."""
    lib_root = Path(lib_root).resolve()
    project_dir = Path(project_dir).resolve()
    out = Path(out_dir).resolve() if out_dir else (project_dir / "project_libs")
    rel_out = _relative_out(project_dir, out)

    lib = lib if lib is not None else lb.scan(lib_root)
    plan = ops.Plan(root=project_dir, title=f"Package custom libraries for {project_dir.name}")
    result = PackageResult(project_dir=project_dir, out_dir=out, rel_out=rel_out)

    used_syms, used_fps = find_used_libraries(project_dir)
    prefix = f"{rel_out}/" if rel_out else ""

    categories: Set[str] = set()

    # --- symbols ---------------------------------------------------------
    for lib_id in sorted(used_syms):
        cat, name = lib_id.split(":", 1)
        sym = lib.find_symbol(cat, name)
        if sym is None or sym.error:
            result.unresolved_symbols.append(lib_id)
            continue
        target = out / "symbols" / f"{cat}{lb.SYMDIR_SUFFIX}" / sym.path.name
        plan.copy(sym.path, target, note=f"symbol {lib_id}")
        result.packaged_symbols.append(lib_id)
        categories.add(cat)

        # A derived symbol is useless without its parent, which lives in a
        # sibling file the project never names directly.
        if sym.extends:
            parent = lib.find_symbol(cat, sym.extends)
            if parent is None:
                plan.warn(
                    f"'{lib_id}' extends '{sym.extends}', which is not in the "
                    f"library; the packaged symbol will not load"
                )
            else:
                parent_id = parent.lib_id
                if parent_id not in result.packaged_symbols:
                    plan.copy(
                        parent.path,
                        out / "symbols" / f"{cat}{lb.SYMDIR_SUFFIX}" / parent.path.name,
                        note=f"parent of {name}, required by (extends)",
                    )
                    result.packaged_symbols.append(parent_id)
                    plan.note(f"also packaged '{parent_id}' because '{name}' extends it")

    # --- footprints and their models -------------------------------------
    for lib_id in sorted(used_fps):
        cat, name = lib_id.split(":", 1)
        fp = lib.find_footprint(cat, name)
        if fp is None or fp.error:
            result.unresolved_footprints.append(lib_id)
            continue

        try:
            text = sx.read_text(fp.path)
        except OSError as exc:
            plan.warn(f"cannot read {lib.rel(fp.path)}: {exc}")
            result.unresolved_footprints.append(lib_id)
            continue

        # Resolve models from the footprint's own paths -- never by globbing.
        for index, raw in enumerate(fp.model_paths):
            info = lb.classify_model_path(lib_root, raw)
            if not info.is_canonical:
                plan.warn(
                    f"footprint '{lib_id}' has a model path that is {info.kind} "
                    f"({raw!r}); it is left as-is and will not resolve in the bundle"
                )
                continue
            if info.resolved is None or not info.resolved.exists():
                plan.warn(
                    f"footprint '{lib_id}' references a model that is missing "
                    f"from the library: {raw!r}"
                )
                continue
            model_target = out / "3dmodels" / f"{cat}{lb.SHAPES_SUFFIX}" / info.resolved.name
            plan.copy(info.resolved, model_target, note=f"3D model for {name}")
            result.packaged_models.append(f"{cat}/{info.resolved.name}")
            text = sx.set_model_path(
                text, index,
                f"{PRJ_VAR}/{prefix}3dmodels/{cat}{lb.SHAPES_SUFFIX}/{info.resolved.name}",
            )

        plan.write(
            out / "footprints" / f"{cat}{lb.PRETTY_SUFFIX}" / fp.path.name,
            text,
            note=f"footprint {lib_id}" + (" (model paths rewritten)" if fp.model_paths else ""),
        )
        result.packaged_footprints.append(lib_id)
        categories.add(cat)

    result.categories = sorted(categories, key=lambda n: (n.lower(), n))

    if not categories:
        plan.note(
            "the project uses no components from this custom library; nothing to package"
        )
        return plan, result

    # --- project tables, at the project root ------------------------------
    sym_entries = [
        tg.LibEntry(
            name=cat,
            uri=f"{PRJ_VAR}/{prefix}symbols/{cat}{lb.SYMDIR_SUFFIX}",
            descr=f"Packaged library {cat}",
        )
        for cat in result.categories
        if any(s.startswith(f"{cat}:") for s in result.packaged_symbols)
    ]
    fp_entries = [
        tg.LibEntry(
            name=cat,
            uri=f"{PRJ_VAR}/{prefix}footprints/{cat}{lb.PRETTY_SUFFIX}",
            descr=f"Packaged library {cat}",
        )
        for cat in result.categories
        if any(f.startswith(f"{cat}:") for f in result.packaged_footprints)
    ]

    for filename, root_tag, entries in (
        (tg.SYM_TABLE_NAME, "sym_lib_table", sym_entries),
        (tg.FP_TABLE_NAME, "fp_lib_table", fp_entries),
    ):
        if not entries:
            continue
        table_path = project_dir / filename
        existing = sx.read_text(table_path) if table_path.exists() else None
        merged, added = merge_table(existing, root_tag, entries)
        if existing is not None and added:
            plan.copy(table_path, table_path.with_suffix(table_path.suffix + ".bak"),
                      note="backup of the original project table")
        if merged != existing:
            plan.write(table_path, merged,
                       note=f"added {len(added)} library row(s), existing rows kept")
            result.tables_updated.append(filename)
        elif existing is not None:
            plan.note(f"{filename} already lists every packaged library")

    for cat in result.categories:
        official = naming.official_collision(cat)
        if official:
            plan.warn(
                f"packaged library '{cat}' has the same nickname as the official "
                f"KiCad library '{official}'; the project will have two libraries "
                f"with one nickname and which wins is not well defined"
            )

    return plan, result


# --------------------------------------------------------------------------
# Convenience
# --------------------------------------------------------------------------

class ProjectPackager:
    """Thin wrapper kept for the existing CLI and GUI call sites."""

    def __init__(self, lib_root: Path):
        self.lib_root = Path(lib_root).resolve()

    def find_used_libraries_in_project(self, project_dir: Path) -> Tuple[Set[str], Set[str]]:
        return find_used_libraries(project_dir)

    def plan(
        self, project_dir: Path, output_dir: Optional[Path] = None
    ) -> Tuple[ops.Plan, PackageResult]:
        return plan_package(self.lib_root, project_dir, output_dir)

    def package_for_project(
        self, project_dir: Path, output_dir: Optional[Path] = None
    ) -> Dict[str, object]:
        plan, result = self.plan(project_dir, output_dir)
        applied = ops.apply(plan)
        if not applied.ok:
            op, err = applied.failed
            raise ops.ApplyError(f"packaging failed at {op.describe(plan.root)}: {err}")
        return {
            "output_directory": str(result.out_dir),
            "packaged_categories": result.categories,
            "packaged_symbols": result.packaged_symbols,
            "packaged_footprints": result.packaged_footprints,
            "packaged_models": result.packaged_models,
            "unresolved_symbols": result.unresolved_symbols,
            "unresolved_footprints": result.unresolved_footprints,
            "tables_updated": result.tables_updated,
        }
