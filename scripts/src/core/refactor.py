"""
Renaming and moving, with library-wide reference rewriting.

Every operation here scans the *whole* library, because a reference can cross
a category boundary: a symbol in category A may legitimately point at a
footprint in category B, and both point at models by path. Rewriting only the
category being edited would leave the others dangling.

The case that is easy to get wrong is derived symbols. `(extends "PARENT")`
always names a sibling *file* in the same .kicad_symdir -- 12249 of them in
the official libraries, none in-file (AGENTS.md 6.2) -- so renaming a symbol
has to rewrite its siblings, and moving one out of its symdir orphans any
sibling that extends it.

Renaming anything breaks projects that already reference the old name. That
is unavoidable and always warned about; KiCad's "Edit Symbol Library
References" dialog is the fix on the project side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import library as lb
from . import naming
from . import ops
from . import provenance as pv
from . import s_expr as sx

KIND_SYMBOL = pv.KIND_SYMBOL
KIND_FOOTPRINT = pv.KIND_FOOTPRINT
KIND_MODEL = pv.KIND_MODEL
KIND_CATEGORY = "category"


class RefactorError(ValueError):
    """The requested rename or move cannot be planned."""


def _break_warning(what: str, old: str, new: str) -> str:
    return (
        f"any existing KiCad project referencing {what} '{old}' will break; "
        f"after this, open the project and use Tools > Edit Symbol Library "
        f"References (or Change Footprints) to point it at '{new}'"
    )


# --------------------------------------------------------------------------
# Edit accumulator
# --------------------------------------------------------------------------

@dataclass
class _Workspace:
    """
    Accumulates per-file content and path changes before emitting operations.

    Needed because one refactor can touch the same file twice -- a symbol
    being renamed may also have its Footprint reference rewritten -- and two
    independent WriteText operations to the same path would silently discard
    the first one's change.
    """
    root: Path
    texts: Dict[Path, str] = field(default_factory=dict)
    moves: Dict[Path, Path] = field(default_factory=dict)
    copies: List[Tuple[Path, Path]] = field(default_factory=list)
    notes: Dict[Path, str] = field(default_factory=dict)

    def text(self, path: Path) -> str:
        if path not in self.texts:
            self.texts[path] = sx.read_text(path)
        return self.texts[path]

    def set_text(self, path: Path, text: str, note: str = "") -> None:
        self.texts[path] = text
        if note:
            self.notes[path] = note

    def move(self, old: Path, new: Path, note: str = "") -> None:
        self.moves[old] = new
        if note:
            self.notes[old] = note

    def binary_move(self, old: Path, new: Path, note: str = "") -> None:
        """A file whose content is not rewritten, e.g. a STEP model."""
        self.copies.append((old, new))
        if note:
            self.notes[old] = note

    def emit(self, plan: ops.Plan) -> None:
        """
        Write first, delete afterwards.

        A failure part-way therefore leaves the old file still present rather
        than losing it, and every write is individually atomic.
        """
        for old, new in self.copies:
            plan.move(old, new, note=self.notes.get(old, ""))

        touched = sorted(set(self.texts) | set(self.moves), key=lambda p: p.as_posix())
        deletions: List[Path] = []
        for path in touched:
            target = self.moves.get(path, path)
            if path in self.texts:
                text = self.texts[path]
            elif path.exists():
                text = sx.read_text(path)
            else:
                continue
            plan.write(target, text, note=self.notes.get(path, ""))
            if target != path:
                deletions.append(path)

        for path in deletions:
            plan.delete(path, note="replaced by the renamed file")


# --------------------------------------------------------------------------
# Shared reference rewriting
# --------------------------------------------------------------------------

def _retarget_footprint_refs(
    ws: _Workspace, lib: lb.Library, old_id: str, new_id: str
) -> List[str]:
    """Point every symbol whose Footprint is `old_id` at `new_id`."""
    changed: List[str] = []
    for sym in lib.symbols:
        if (sym.footprint_ref or "").strip() != old_id or sym.error:
            continue
        text = ws.text(sym.path)
        internal = sym.internal_name or sym.name
        try:
            ws.set_text(
                sym.path,
                sx.set_symbol_footprint(text, internal, new_id),
                note=f"Footprint -> {new_id}",
            )
        except sx.SExprError:
            continue
        changed.append(sym.lib_id)
    return changed


def _retarget_model_paths(
    ws: _Workspace, lib: lb.Library, old_uri: str, new_uri: str
) -> List[str]:
    """Repoint every footprint model path equal to `old_uri`."""
    changed: List[str] = []
    for fp in lib.footprints:
        if fp.error:
            continue
        hits = [i for i, raw in enumerate(fp.model_paths) if raw == old_uri]
        if not hits:
            continue
        text = ws.text(fp.path)
        for i in hits:
            text = sx.set_model_path(text, i, new_uri)
        ws.set_text(fp.path, text, note=f"model path -> {Path(new_uri).name}")
        changed.append(fp.lib_id)
    return changed


def _retarget_extends(
    ws: _Workspace, lib: lb.Library, category: str, old: str, new: str
) -> List[str]:
    """
    Rewrite `(extends "old")` in every sibling file of the symdir.

    This is the cross-file case: the parent is never defined in the same file
    as the derived symbol, so an in-file-only rewrite would silently break
    every variant of a part.
    """
    changed: List[str] = []
    for sym in lib.symbols_in(category):
        if sym.extends != old or sym.error:
            continue
        text, count = sx.retarget_extends(ws.text(sym.path), old, new)
        if count:
            ws.set_text(sym.path, text, note=f"extends -> {new}")
            changed.append(sym.lib_id)
    return changed


def _check_target_free(
    plan: ops.Plan, target: Path, conflict: ops.ConflictPolicy, what: str
) -> Path:
    if not target.exists():
        return target
    if conflict is ops.ConflictPolicy.OVERWRITE:
        plan.warn(f"overwriting existing {what} '{target.name}'")
        return target
    if conflict is ops.ConflictPolicy.RENAME:
        final = ops.unique_target(target)
        plan.warn(f"{what} {target.name} exists; using {final.name} instead")
        return final
    raise RefactorError(
        f"{what} '{target.name}' already exists; choose a different name or pass "
        f"a conflict policy (overwrite / rename)"
    )


# --------------------------------------------------------------------------
# Rename a symbol
# --------------------------------------------------------------------------

def plan_rename_symbol(
    root: Path,
    category: str,
    old: str,
    new: str,
    *,
    conflict: ops.ConflictPolicy = ops.ConflictPolicy.SKIP,
    prov: Optional[pv.Provenance] = None,
    lib: Optional[lb.Library] = None,
) -> ops.Plan:
    """Rename a symbol: its file, its internal names, and sibling extends."""
    root = Path(root).resolve()
    lib = lib if lib is not None else lb.scan(root)
    naming.validate(new, "symbol")

    sym = lib.find_symbol(category, old)
    if sym is None:
        raise RefactorError(f"no symbol '{old}' in category '{category}'")
    if sym.error:
        raise RefactorError(f"symbol '{old}' cannot be parsed: {sym.error}")
    if new == old:
        return ops.Plan(root=root, title="Nothing to rename")

    plan = ops.Plan(root=root, title=f"Rename symbol {category}:{old} -> {new}")
    ws = _Workspace(root=root)

    target = _check_target_free(
        plan, sym.path.with_name(f"{new}{lb.SYMBOL_EXT}"), conflict, "symbol"
    )
    final_name = target.stem

    internal = sym.internal_name or old
    ws.set_text(
        sym.path,
        sx.rename_symbol(ws.text(sym.path), internal, final_name),
        note=f"internal name, unit sub-symbols and Value -> {final_name}",
    )
    ws.move(sym.path, target)

    derived = _retarget_extends(ws, lib, category, internal, final_name)
    if derived:
        plan.note(
            f"updated (extends \"{internal}\") in {len(derived)} sibling file(s): "
            + ", ".join(derived)
        )

    ws.emit(plan)
    if prov is not None:
        prov.rename(category, KIND_SYMBOL, old, final_name)
    plan.warn(_break_warning("symbol", f"{category}:{old}", f"{category}:{final_name}"))
    return plan


# --------------------------------------------------------------------------
# Rename a footprint
# --------------------------------------------------------------------------

def plan_rename_footprint(
    root: Path,
    category: str,
    old: str,
    new: str,
    *,
    rename_model: bool = False,
    conflict: ops.ConflictPolicy = ops.ConflictPolicy.SKIP,
    prov: Optional[pv.Provenance] = None,
    lib: Optional[lb.Library] = None,
) -> ops.Plan:
    """
    Rename a footprint and every symbol that points at it.

    With rename_model=True the attached 3D model file is renamed to match,
    keeping the convention that a model is named after its footprint.
    """
    root = Path(root).resolve()
    lib = lib if lib is not None else lb.scan(root)
    naming.validate(new, "footprint")

    fp = lib.find_footprint(category, old)
    if fp is None:
        raise RefactorError(f"no footprint '{old}' in category '{category}'")
    if fp.error:
        raise RefactorError(f"footprint '{old}' cannot be parsed: {fp.error}")
    if new == old:
        return ops.Plan(root=root, title="Nothing to rename")

    plan = ops.Plan(root=root, title=f"Rename footprint {category}:{old} -> {new}")
    ws = _Workspace(root=root)

    target = _check_target_free(
        plan, fp.path.with_name(f"{new}{lb.FOOTPRINT_EXT}"), conflict, "footprint"
    )
    final_name = target.stem

    text = sx.rename_footprint(ws.text(fp.path), fp.internal_name or old, final_name)
    ws.set_text(fp.path, text, note=f"internal name -> {final_name}")
    ws.move(fp.path, target)

    if rename_model:
        for index, raw in enumerate(fp.model_paths):
            info = lb.classify_model_path(root, raw)
            if not info.is_canonical or info.resolved is None or not info.resolved.exists():
                plan.warn(
                    f"cannot rename the model for '{old}': {raw!r} does not resolve"
                )
                continue
            suffix = info.resolved.suffix
            new_model = info.resolved.with_name(f"{final_name}{suffix}")
            new_model = _check_target_free(plan, new_model, conflict, "3D model")
            ws.binary_move(info.resolved, new_model, note="renamed to match its footprint")
            new_uri = (
                f"{lb.LIB_VAR_PREFIX}/3dmodels/{category}{lb.SHAPES_SUFFIX}/{new_model.name}"
            )
            ws.set_text(
                fp.path, sx.set_model_path(ws.text(fp.path), index, new_uri),
                note=f"model path -> {new_model.name}",
            )
            if prov is not None:
                prov.rename(category, KIND_MODEL, info.resolved.name, new_model.name)

    users = _retarget_footprint_refs(ws, lib, f"{category}:{old}", f"{category}:{final_name}")
    if users:
        plan.note(f"updated the Footprint property of {len(users)} symbol(s): "
                  + ", ".join(users))
    else:
        plan.note("no symbol in this library referenced that footprint")

    ws.emit(plan)
    if prov is not None:
        prov.rename(category, KIND_FOOTPRINT, old, final_name)
    plan.warn(_break_warning("footprint", f"{category}:{old}", f"{category}:{final_name}"))
    return plan


# --------------------------------------------------------------------------
# Rename a 3D model file
# --------------------------------------------------------------------------

def plan_rename_model(
    root: Path,
    category: str,
    old: str,
    new: str,
    *,
    conflict: ops.ConflictPolicy = ops.ConflictPolicy.SKIP,
    prov: Optional[pv.Provenance] = None,
    lib: Optional[lb.Library] = None,
) -> ops.Plan:
    """Rename a model file and repoint every footprint that uses it."""
    root = Path(root).resolve()
    lib = lib if lib is not None else lb.scan(root)

    model = lib.find_model(category, old)
    if model is None:
        raise RefactorError(f"no 3D model '{old}' in category '{category}'")

    # The extension is part of a model's identity; keep the old one if omitted.
    new_name = new if Path(new).suffix else f"{new}{model.path.suffix}"
    naming.validate(Path(new_name).stem, "3D model")
    if new_name == old:
        return ops.Plan(root=root, title="Nothing to rename")

    plan = ops.Plan(root=root, title=f"Rename 3D model {category}/{old} -> {new_name}")
    ws = _Workspace(root=root)

    target = _check_target_free(plan, model.path.with_name(new_name), conflict, "3D model")
    ws.binary_move(model.path, target)

    old_uri = model.rel_uri
    new_uri = f"{lb.LIB_VAR_PREFIX}/3dmodels/{category}{lb.SHAPES_SUFFIX}/{target.name}"
    users = _retarget_model_paths(ws, lib, old_uri, new_uri)
    if users:
        plan.note(f"repointed {len(users)} footprint(s): " + ", ".join(users))
    else:
        plan.note("no footprint referenced that model")

    ws.emit(plan)
    if prov is not None:
        prov.rename(category, KIND_MODEL, old, target.name)
    return plan


# --------------------------------------------------------------------------
# Move an item to another category
# --------------------------------------------------------------------------

def plan_move(
    root: Path,
    kind: str,
    category: str,
    name: str,
    new_category: str,
    *,
    conflict: ops.ConflictPolicy = ops.ConflictPolicy.SKIP,
    prov: Optional[pv.Provenance] = None,
    lib: Optional[lb.Library] = None,
) -> ops.Plan:
    """
    Move one symbol, footprint or model into another category.

    Only the directory changes -- the item keeps its name -- but every
    reference to it has to be rewritten because a reference carries the
    category as its lib_id prefix or in its model path.
    """
    root = Path(root).resolve()
    lib = lib if lib is not None else lb.scan(root)
    naming.validate(new_category, "category")

    if new_category == category:
        return ops.Plan(root=root, title="Nothing to move")

    plan = ops.Plan(root=root, title=f"Move {kind} {category}:{name} -> {new_category}")
    for w in naming.check_category(new_category, existing=lib.category_names()):
        plan.warn(w)
    ws = _Workspace(root=root)

    if kind == KIND_SYMBOL:
        sym = lib.find_symbol(category, name)
        if sym is None:
            raise RefactorError(f"no symbol '{name}' in category '{category}'")

        dependants = [s.lib_id for s in lib.symbols_in(category)
                      if s.extends == (sym.internal_name or name) and s.name != name]
        if dependants:
            raise RefactorError(
                f"symbol '{name}' is the parent of {len(dependants)} derived symbol(s) "
                f"in '{category}' ({', '.join(dependants)}). Moving it would leave them "
                f"with no parent in their own symdir, which KiCad cannot resolve. "
                f"Move the whole family, or rename instead."
            )

        target_dir = root / "symbols" / f"{new_category}{lb.SYMDIR_SUFFIX}"
        target = _check_target_free(
            plan, target_dir / sym.path.name, conflict, "symbol"
        )
        ws.move(sym.path, target, note=f"moved to {new_category}")
        if sym.extends:
            plan.warn(
                f"'{name}' extends '{sym.extends}', which stays in '{category}'. "
                f"A derived symbol's parent must be a sibling file in the same "
                f"symdir, so move '{sym.extends}' as well or the symbol will not load."
            )
        if prov is not None:
            prov.rename(category, KIND_SYMBOL, name, target.stem, new_category=new_category)

    elif kind == KIND_FOOTPRINT:
        fp = lib.find_footprint(category, name)
        if fp is None:
            raise RefactorError(f"no footprint '{name}' in category '{category}'")

        target_dir = root / "footprints" / f"{new_category}{lb.PRETTY_SUFFIX}"
        target = _check_target_free(plan, target_dir / fp.path.name, conflict, "footprint")
        ws.move(fp.path, target, note=f"moved to {new_category}")

        users = _retarget_footprint_refs(
            ws, lib, f"{category}:{name}", f"{new_category}:{target.stem}"
        )
        plan.note(
            f"updated the Footprint property of {len(users)} symbol(s): " + ", ".join(users)
            if users else "no symbol in this library referenced that footprint"
        )
        for raw in fp.model_paths:
            info = lb.classify_model_path(root, raw)
            if info.is_canonical and f"/{category}{lb.SHAPES_SUFFIX}/" in raw:
                plan.warn(
                    f"footprint '{name}' still points at its model in the old "
                    f"category ({raw}); move the model too, or the 3D view breaks"
                )
        if prov is not None:
            prov.rename(category, KIND_FOOTPRINT, name, target.stem, new_category=new_category)

    elif kind == KIND_MODEL:
        model = lib.find_model(category, name)
        if model is None:
            raise RefactorError(f"no 3D model '{name}' in category '{category}'")

        target_dir = root / "3dmodels" / f"{new_category}{lb.SHAPES_SUFFIX}"
        target = _check_target_free(plan, target_dir / model.path.name, conflict, "3D model")
        ws.binary_move(model.path, target, note=f"moved to {new_category}")
        new_uri = (
            f"{lb.LIB_VAR_PREFIX}/3dmodels/{new_category}{lb.SHAPES_SUFFIX}/{target.name}"
        )
        users = _retarget_model_paths(ws, lib, model.rel_uri, new_uri)
        plan.note(
            f"repointed {len(users)} footprint(s): " + ", ".join(users)
            if users else "no footprint referenced that model"
        )
        if prov is not None:
            prov.rename(category, KIND_MODEL, name, target.name, new_category=new_category)

    else:
        raise RefactorError(f"unknown kind {kind!r}; expected symbol, footprint or model")

    ws.emit(plan)
    _cleanup_empty_dirs(plan, root, category)
    if kind != KIND_MODEL:
        plan.warn(_break_warning(kind, f"{category}:{name}", f"{new_category}:{name}"))
    return plan


# --------------------------------------------------------------------------
# Rename a category
# --------------------------------------------------------------------------

def plan_rename_category(
    root: Path,
    old: str,
    new: str,
    *,
    conflict: ops.ConflictPolicy = ops.ConflictPolicy.SKIP,
    prov: Optional[pv.Provenance] = None,
    lib: Optional[lb.Library] = None,
) -> ops.Plan:
    """
    Rename a category: all three directories, every lib_id prefix, every
    model path.
    """
    root = Path(root).resolve()
    lib = lib if lib is not None else lb.scan(root)

    if old not in lib.categories:
        raise RefactorError(f"no category '{old}'")
    if new == old:
        return ops.Plan(root=root, title="Nothing to rename")
    naming.validate(new, "category")
    if new in lib.categories and conflict is ops.ConflictPolicy.SKIP:
        raise RefactorError(
            f"category '{new}' already exists; merging categories is not "
            f"supported here -- move the items individually, or pass "
            f"--conflict overwrite"
        )

    plan = ops.Plan(root=root, title=f"Rename category {old} -> {new}")
    for w in naming.check_category(new, existing=[c for c in lib.category_names() if c != old]):
        plan.warn(w)
    ws = _Workspace(root=root)

    for sym in lib.symbols_in(old):
        target = root / "symbols" / f"{new}{lb.SYMDIR_SUFFIX}" / sym.path.name
        ws.move(sym.path, target, note=f"symbol moved to {new}")

    for fp in lib.footprints_in(old):
        target = root / "footprints" / f"{new}{lb.PRETTY_SUFFIX}" / fp.path.name
        ws.move(fp.path, target, note=f"footprint moved to {new}")

    for model in lib.models_in(old):
        target = root / "3dmodels" / f"{new}{lb.SHAPES_SUFFIX}" / model.path.name
        ws.binary_move(model.path, target, note=f"model moved to {new}")

    # Rewrite every reference in the whole library, not just this category:
    # a symbol elsewhere may point at a footprint being renamed.
    ref_count = 0
    for fp in lib.footprints_in(old):
        ref_count += len(_retarget_footprint_refs(ws, lib, f"{old}:{fp.name}", f"{new}:{fp.name}"))
    for model in lib.models_in(old):
        new_uri = f"{lb.LIB_VAR_PREFIX}/3dmodels/{new}{lb.SHAPES_SUFFIX}/{model.filename}"
        ref_count += len(_retarget_model_paths(ws, lib, model.rel_uri, new_uri))

    ws.emit(plan)
    _cleanup_empty_dirs(plan, root, old)

    if prov is not None:
        prov.rename_category(old, new)

    plan.note(f"rewrote {ref_count} cross-reference(s)")
    plan.warn(_break_warning("library", old, new))
    plan.warn(
        f"the library nickname changes from '{old}' to '{new}', so every "
        f"lib_id in every project that uses it changes too"
    )
    return plan


def _cleanup_empty_dirs(plan: ops.Plan, root: Path, category: str) -> None:
    """Remove the three mirror directories if the move emptied them."""
    for sub, suffix in (
        ("symbols", lb.SYMDIR_SUFFIX),
        ("footprints", lb.PRETTY_SUFFIX),
        ("3dmodels", lb.SHAPES_SUFFIX),
    ):
        plan.delete_dir_if_empty(
            root / sub / f"{category}{suffix}", note="emptied by this operation"
        )
