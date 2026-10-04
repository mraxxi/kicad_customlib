"""
Candidate detection and import planning.

The old ingest_part() took `[0]` of each file list, so a multi-part archive
silently lost every part but the first, `rglob` happily matched macOS
`__MACOSX/._*` resource forks, and picking a lone `.kicad_mod` made it search
*inside a file*, copy nothing, and still create three empty category
directories plus a manifest record with `files: {}`.

This module instead finds every candidate, names each one, suggests pairings
the caller may override, and returns a Plan. Nothing is written until the
plan is applied.

Two naming decisions matter:

* A 3D model is named after its **footprint**, not after the symbol. A model
  belongs to a footprint, and the packager finds it by reading the
  footprint's own (model ...) path.
* A multi-symbol .kicad_sym is split into one file per symbol, because every
  one of the 22784 official symbol files holds exactly one (AGENTS.md 6.1).

Planning records provenance intent on the plan rather than writing it, so
previewing an import and then cancelling leaves provenance untouched.
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import library as lb
from . import naming
from . import ops
from . import provenance as pv
from . import s_expr as sx

KIND_SYMBOL = "symbol"
KIND_FOOTPRINT = "footprint"
KIND_MODEL = "model"

# Resource forks, Finder metadata and Windows thumbnails are not KiCad files.
_JUNK_DIRS = {"__MACOSX", "__MACOS"}
_JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def _is_junk(rel_parts: Sequence[str]) -> bool:
    for part in rel_parts:
        if part in _JUNK_DIRS or part in _JUNK_NAMES:
            return True
        if part.startswith("._"):
            return True
    return False


# --------------------------------------------------------------------------
# Getting at the source files
# --------------------------------------------------------------------------

@dataclass
class Bundle:
    """A directory containing the files to import, plus what was ignored."""
    root: Path
    source: Path
    temp_dir: Optional[Path] = None
    ignored: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    explicit_files: Optional[List[Path]] = None

    def close(self) -> None:
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = None

    def __enter__(self) -> "Bundle":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _safe_members(zf: zipfile.ZipFile, bundle: Bundle) -> List[zipfile.ZipInfo]:
    """
    Members that are safe to extract and worth extracting.

    Rejects absolute paths and any '..' traversal (zip-slip) rather than
    trusting extractall.
    """
    out: List[zipfile.ZipInfo] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        parts = [p for p in name.split("/") if p not in ("", ".")]

        if name.startswith("/") or ".." in parts or (len(name) > 1 and name[1] == ":"):
            bundle.warnings.append(
                f"refused unsafe archive member {info.filename!r} (escapes the "
                f"extraction directory)"
            )
            continue
        if _is_junk(parts):
            bundle.ignored.append(info.filename)
            continue
        out.append(info)
    return out


def open_bundle(source: Path) -> Bundle:
    """
    Make the source importable: extract a ZIP to a temp directory, or use a
    folder or individual file as-is. Always close() the result -- or use it as
    a context manager -- so the temp directory cannot leak.
    """
    source = Path(source).resolve()
    if not source.exists():
        raise FileNotFoundError(f"source does not exist: {source}")

    if source.is_file() and zipfile.is_zipfile(source):
        temp = Path(tempfile.mkdtemp(prefix="kicad_ingest_"))
        bundle = Bundle(root=temp, source=source, temp_dir=temp)
        try:
            with zipfile.ZipFile(source) as zf:
                members = _safe_members(zf, bundle)
                for info in members:
                    zf.extract(info, temp)
        except BaseException:
            bundle.close()
            raise
        return bundle

    if source.is_dir():
        return Bundle(root=source, source=source)

    # A single file: a lone .kicad_mod is a legitimate thing to import, and
    # must not be treated as a directory to search inside.
    return Bundle(root=source.parent, source=source, explicit_files=[source])


# --------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    """One importable item. `target_name` is what the caller may edit."""
    kind: str
    source_file: Path
    detected_name: str
    target_name: str
    include: bool = True
    internal_name: Optional[str] = None
    symbol_of: Optional[str] = None        # set when split from a multi-symbol file
    source_footprint_ref: Optional[str] = None
    source_model_paths: Tuple[str, ...] = ()
    notes: List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Stable identity within one ingest run."""
        return f"{self.kind}|{self.detected_name}|{self.source_file.name}"

    @property
    def extension(self) -> str:
        return self.source_file.suffix

    def describe(self) -> str:
        line = f"{self.kind:<10} {self.detected_name}"
        if self.target_name != self.detected_name:
            line += f"  ->  {self.target_name}"
        if self.notes:
            line += f"   ({'; '.join(self.notes)})"
        return line


def _candidate_files(bundle: Bundle) -> List[Path]:
    if bundle.explicit_files is not None:
        return list(bundle.explicit_files)
    out: List[Path] = []
    for p in sorted(bundle.root.rglob("*")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(bundle.root).parts
        if _is_junk(rel_parts):
            bundle.ignored.append("/".join(rel_parts))
            continue
        out.append(p)
    return out


def _symbol_candidates(path: Path, bundle: Bundle) -> List[Candidate]:
    try:
        text = sx.read_text(path)
        tops = sx.top_level_symbols(text)
    except (OSError, sx.SExprError) as exc:
        bundle.warnings.append(f"{path.name}: cannot read as a symbol library ({exc})")
        return []

    if not tops:
        bundle.warnings.append(f"{path.name}: declares no symbol; not imported")
        return []

    multi = len(tops) > 1
    out: List[Candidate] = []
    for name, open_idx, _close in tops:
        target = naming.sanitize(name)
        cand = Candidate(
            kind=KIND_SYMBOL,
            source_file=path,
            detected_name=name,
            target_name=target,
            internal_name=name,
            symbol_of=name if multi else None,
            source_footprint_ref=sx.get_property(text, open_idx, "Footprint") or None,
        )
        if multi:
            cand.notes.append(f"split from {path.name} ({len(tops)} symbols)")
        if target != name:
            cand.notes.append(f"renamed from {name!r} (illegal characters)")
        out.append(cand)
    return out


def _footprint_candidate(path: Path, bundle: Bundle) -> Optional[Candidate]:
    try:
        text = sx.read_text(path)
        name, _open, _close = sx.footprint_span(text)
        models = sx.all_models(text)
    except (OSError, sx.SExprError) as exc:
        bundle.warnings.append(f"{path.name}: cannot read as a footprint ({exc})")
        return None

    target = naming.sanitize(name)
    cand = Candidate(
        kind=KIND_FOOTPRINT,
        source_file=path,
        detected_name=name,
        target_name=target,
        internal_name=name,
        source_model_paths=tuple(m.path for m in models),
    )
    if target != name:
        cand.notes.append(f"renamed from {name!r} (illegal characters)")
    if not models:
        cand.notes.append("no 3D model block")
    return cand


def detect(bundle: Bundle) -> List[Candidate]:
    """
    Every importable item in the bundle.

    A multi-symbol library yields one candidate per symbol. Files that are
    not symbols, footprints or models are ignored without comment; files that
    look right but cannot be parsed produce a warning on the bundle.
    """
    candidates: List[Candidate] = []
    for path in _candidate_files(bundle):
        suffix = path.suffix.lower()
        if suffix == lb.SYMBOL_EXT:
            candidates.extend(_symbol_candidates(path, bundle))
        elif suffix == lb.FOOTPRINT_EXT:
            cand = _footprint_candidate(path, bundle)
            if cand:
                candidates.append(cand)
        elif suffix in lb.MODEL_EXTS:
            candidates.append(Candidate(
                kind=KIND_MODEL,
                source_file=path,
                detected_name=path.name,
                target_name=path.name,
            ))

    order = {KIND_SYMBOL: 0, KIND_FOOTPRINT: 1, KIND_MODEL: 2}
    candidates.sort(key=lambda c: (order[c.kind], c.detected_name.lower(), c.detected_name))
    return candidates


# --------------------------------------------------------------------------
# Auto-pairing
# --------------------------------------------------------------------------

@dataclass
class Pairing:
    """Suggested links between candidates. The caller may override any of them."""
    symbol_to_footprint: Dict[str, Optional[str]] = field(default_factory=dict)
    footprint_to_model: Dict[str, Optional[str]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


def _basename_after_colon(ref: str) -> str:
    return ref.split(":", 1)[-1].strip()


def autopair(candidates: Sequence[Candidate]) -> Pairing:
    """
    Suggest symbol -> footprint -> model links.

    In order of confidence: an explicit reference already in the file, then a
    matching stem, then -- only when there is exactly one of each -- the
    obvious pairing. Anything left unpaired is reported rather than guessed.
    """
    pairing = Pairing()
    symbols = [c for c in candidates if c.kind == KIND_SYMBOL]
    footprints = [c for c in candidates if c.kind == KIND_FOOTPRINT]
    models = [c for c in candidates if c.kind == KIND_MODEL]

    fp_by_detected = {c.detected_name: c for c in footprints}
    fp_by_sanitized = {naming.sanitize(c.detected_name): c for c in footprints}

    for sym in symbols:
        chosen: Optional[Candidate] = None
        if sym.source_footprint_ref:
            wanted = _basename_after_colon(sym.source_footprint_ref)
            chosen = fp_by_detected.get(wanted) or fp_by_sanitized.get(naming.sanitize(wanted))
        if chosen is None:
            chosen = fp_by_detected.get(sym.detected_name) or fp_by_sanitized.get(sym.target_name)
        if chosen is None and len(symbols) == 1 and len(footprints) == 1:
            chosen = footprints[0]
        pairing.symbol_to_footprint[sym.key] = chosen.key if chosen else None
        if chosen is None and footprints:
            pairing.warnings.append(
                f"symbol '{sym.detected_name}' could not be matched to a footprint "
                f"in this bundle; its Footprint property will be left as it is"
            )

    model_by_name = {c.detected_name: c for c in models}
    model_by_stem = {c.source_file.stem: c for c in models}

    for fp in footprints:
        chosen = None
        for raw in fp.source_model_paths:
            wanted = raw.replace("\\", "/").rsplit("/", 1)[-1]
            chosen = model_by_name.get(wanted) or model_by_stem.get(Path(wanted).stem)
            if chosen:
                break
        if chosen is None:
            chosen = model_by_stem.get(fp.detected_name) or model_by_stem.get(fp.target_name)
        if chosen is None and len(footprints) == 1 and len(models) == 1:
            chosen = models[0]
        pairing.footprint_to_model[fp.key] = chosen.key if chosen else None

    paired_models = {k for k in pairing.footprint_to_model.values() if k}
    for m in models:
        if m.key not in paired_models:
            pairing.warnings.append(
                f"3D model '{m.detected_name}' is not referenced by any footprint "
                f"in this bundle and will not be imported"
            )

    return pairing


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def _model_target_name(footprint_target: str, model: Candidate) -> str:
    """
    A model is named after the footprint it belongs to.

    This is what makes the packager able to find it: it reads the footprint's
    own (model ...) path rather than globbing for `<footprint>*.step`, which
    previously missed any model named after the symbol instead.
    """
    return f"{footprint_target}{model.source_file.suffix.lower()}"


def plan_ingest(
    root: Path,
    category: str,
    candidates: Sequence[Candidate],
    *,
    pairing: Optional[Pairing] = None,
    conflict: ops.ConflictPolicy = ops.ConflictPolicy.SKIP,
    bundle: Optional[Bundle] = None,
    lib: Optional[lb.Library] = None,
) -> ops.Plan:
    """
    Build the plan for importing `candidates` into `category`.

    Every file's final content is computed here, so the preview is exact:
    internal names renamed, the symbol's Footprint property set to
    '<Category>:<Footprint>', and the footprint's model path rewritten to
    ${KICAD_CUSTOM_LIB}/3dmodels/<Category>.3dshapes/<file> while preserving
    any hand-tuned offset, scale and rotation.
    """
    root = Path(root).resolve()
    lib = lib if lib is not None else lb.scan(root)
    plan = ops.Plan(root=root, title=f"Import {len(candidates)} item(s) into '{category}'")

    for w in naming.check_category(category, existing=lib.category_names()):
        plan.warn(w)

    selected = [c for c in candidates if c.include]
    if not selected:
        plan.warn("nothing selected to import")
        return plan

    if bundle is not None:
        for w in bundle.warnings:
            plan.warn(w)
        if bundle.ignored:
            plan.note(f"ignored {len(bundle.ignored)} non-KiCad file(s): "
                      + ", ".join(sorted(bundle.ignored)[:5])
                      + (" ..." if len(bundle.ignored) > 5 else ""))

    pairing = pairing if pairing is not None else autopair(selected)
    for w in pairing.warnings:
        plan.warn(w)

    by_key = {c.key: c for c in selected}
    for name in (c.target_name for c in selected):
        try:
            naming.validate(name, "item")
        except naming.NameError_ as exc:
            plan.warn(str(exc))

    for group in naming.find_case_collisions([c.target_name for c in selected]):
        plan.warn(f"items {' and '.join(group)} differ only by case and would "
                  f"collide on Windows and macOS")

    sym_dir = root / "symbols" / f"{category}{lb.SYMDIR_SUFFIX}"
    fp_dir = root / "footprints" / f"{category}{lb.PRETTY_SUFFIX}"
    model_dir = root / "3dmodels" / f"{category}{lb.SHAPES_SUFFIX}"

    # --- models first: footprints need their final filenames ---------------
    model_final: Dict[str, str] = {}
    for fp in (c for c in selected if c.kind == KIND_FOOTPRINT):
        model_key = pairing.footprint_to_model.get(fp.key)
        if not model_key or model_key not in by_key:
            continue
        model = by_key[model_key]
        filename = _model_target_name(fp.target_name, model)
        target = ops.resolve_conflict(plan, model_dir / filename, conflict, what="3D model")
        if target is None:
            continue
        model_final[fp.key] = target.name
        note = "named after its footprint"
        if model.source_file.name != target.name:
            note += f"; was {model.source_file.name}"
        plan.copy(model.source_file, target, note=note)
        plan.record_provenance(
            category, pv.KIND_MODEL, target.name,
            original_name=model.source_file.name, source=_source_label(bundle),
        )

    # --- footprints --------------------------------------------------------
    footprint_final: Dict[str, str] = {}
    for fp in (c for c in selected if c.kind == KIND_FOOTPRINT):
        try:
            text = sx.read_text(fp.source_file)
        except OSError as exc:
            plan.warn(f"{fp.source_file.name}: cannot read ({exc}); skipped")
            continue

        if fp.target_name != fp.internal_name:
            text = sx.rename_footprint(text, fp.internal_name, fp.target_name)

        if fp.key in model_final:
            uri = f"{lb.LIB_VAR_PREFIX}/3dmodels/{category}{lb.SHAPES_SUFFIX}/{model_final[fp.key]}"
            text = sx.set_or_add_model(text, uri)
        elif fp.source_model_paths:
            plan.warn(
                f"footprint '{fp.target_name}' references "
                f"{fp.source_model_paths[0]!r} but no matching model file was "
                f"found in the bundle; the path is left untouched and will not resolve"
            )

        target = ops.resolve_conflict(plan, fp_dir / f"{fp.target_name}{lb.FOOTPRINT_EXT}",
                                      conflict, what="footprint")
        if target is None:
            footprint_final[fp.key] = fp.target_name  # the existing one is reused
            continue
        footprint_final[fp.key] = target.stem
        plan.write(target, text, note=f"footprint '{target.stem}'")
        plan.record_provenance(
            category, pv.KIND_FOOTPRINT, target.stem,
            original_name=fp.source_file.name, source=_source_label(bundle),
        )

    # --- symbols -----------------------------------------------------------
    for sym in (c for c in selected if c.kind == KIND_SYMBOL):
        try:
            text = sx.read_text(sym.source_file)
        except OSError as exc:
            plan.warn(f"{sym.source_file.name}: cannot read ({exc}); skipped")
            continue

        if sym.symbol_of:
            text = sx.extract_symbol_file(text, sym.symbol_of)

        if sym.target_name != sym.internal_name:
            text = sx.rename_symbol(text, sym.internal_name, sym.target_name)

        fp_key = pairing.symbol_to_footprint.get(sym.key)
        if fp_key and fp_key in footprint_final:
            text = sx.set_symbol_footprint(
                text, sym.target_name, f"{category}:{footprint_final[fp_key]}"
            )

        target = ops.resolve_conflict(plan, sym_dir / f"{sym.target_name}{lb.SYMBOL_EXT}",
                                      conflict, what="symbol")
        if target is None:
            continue
        note = f"symbol '{target.stem}'"
        if sym.symbol_of:
            note += f" split from {sym.source_file.name}"
        plan.write(target, text, note=note)
        plan.record_provenance(
            category, pv.KIND_SYMBOL, target.stem,
            original_name=sym.source_file.name, source=_source_label(bundle),
        )

    return plan


def _source_label(bundle: Optional[Bundle]) -> str:
    return bundle.source.name if bundle is not None else ""


# --------------------------------------------------------------------------
# One-call convenience
# --------------------------------------------------------------------------

@dataclass
class IngestPreview:
    """Everything needed to show, confirm and then apply an import."""
    bundle: Bundle
    candidates: List[Candidate]
    pairing: Pairing
    plan: ops.Plan

    def close(self) -> None:
        self.bundle.close()

    def __enter__(self) -> "IngestPreview":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def prepare(
    root: Path,
    source: Path,
    category: str,
    *,
    select: Optional[Iterable[str]] = None,
    conflict: ops.ConflictPolicy = ops.ConflictPolicy.SKIP,
) -> IngestPreview:
    """
    Detect, pair and plan in one call.

    The returned preview owns a temp directory when the source was a ZIP, so
    the caller must close() it -- or use it as a context manager -- *after*
    the plan has been applied, since the plan's copy operations read from it.
    """
    bundle = open_bundle(source)
    try:
        candidates = detect(bundle)
        if select is not None:
            wanted = set(select)
            for c in candidates:
                c.include = c.detected_name in wanted or c.target_name in wanted or c.key in wanted
        pairing = autopair([c for c in candidates if c.include])
        plan = plan_ingest(
            root, category, candidates,
            pairing=pairing, conflict=conflict, bundle=bundle,
        )
    except BaseException:
        bundle.close()
        raise
    return IngestPreview(bundle=bundle, candidates=candidates, pairing=pairing, plan=plan)
