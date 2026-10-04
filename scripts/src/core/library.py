"""
Read-only scanner that turns the three mirrored directory trees into an
in-memory index.

The disk is the source of truth. Nothing here consults provenance.json or any
other database: relationships are *derived*, symbol -> footprint from the
symbol's Footprint property, footprint -> model from its (model ...) paths.
A part is therefore not "one symbol + one footprint + one model" -- several
symbols may share a footprint, and a footprint may carry zero or many models.

Scanning never writes and never raises on a damaged file: an unparseable file
becomes a recorded problem so `check` can report it, rather than taking down
the whole scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import s_expr as sx

SYMDIR_SUFFIX = ".kicad_symdir"
PRETTY_SUFFIX = ".pretty"
SHAPES_SUFFIX = ".3dshapes"

SYMBOL_EXT = ".kicad_sym"
FOOTPRINT_EXT = ".kicad_mod"
MODEL_EXTS = (".step", ".stp", ".wrl")

LIB_VAR = "KICAD_CUSTOM_LIB"
LIB_VAR_PREFIX = "${" + LIB_VAR + "}"


def _sort_key(name: str) -> Tuple[str, str]:
    """Case-insensitive first, exact spelling as tiebreak, so order is stable."""
    return (name.lower(), name)


def is_model_file(path: Path) -> bool:
    return path.suffix.lower() in MODEL_EXTS


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Category:
    """One library nickname and which of its three mirror directories exist."""
    name: str
    has_symbol_dir: bool = False
    has_footprint_dir: bool = False
    has_model_dir: bool = False
    symbol_count: int = 0
    footprint_count: int = 0
    model_count: int = 0

    @property
    def is_empty(self) -> bool:
        """No files anywhere. Git cannot track an empty directory, so these
        appear on one machine and not the other."""
        return (self.symbol_count + self.footprint_count + self.model_count) == 0

    @property
    def missing_mirrors(self) -> List[str]:
        """Which of the three directories are absent."""
        out = []
        if not self.has_symbol_dir:
            out.append(f"symbols/{self.name}{SYMDIR_SUFFIX}")
        if not self.has_footprint_dir:
            out.append(f"footprints/{self.name}{PRETTY_SUFFIX}")
        if not self.has_model_dir:
            out.append(f"3dmodels/{self.name}{SHAPES_SUFFIX}")
        return out


@dataclass(frozen=True)
class Symbol:
    category: str
    name: str              # filename stem -- what a lib_id refers to
    path: Path
    internal_name: Optional[str] = None   # the (symbol "...") declaration
    footprint_ref: Optional[str] = None   # "Cat:Name", or None/"" when unset
    extends: Optional[str] = None         # parent symbol, a sibling file
    extra_symbols: Tuple[str, ...] = ()   # 2nd+ top-level symbols, if any
    error: Optional[str] = None

    @property
    def lib_id(self) -> str:
        return f"{self.category}:{self.name}"

    @property
    def name_matches_internal(self) -> bool:
        return self.internal_name is None or self.internal_name == self.name

    @property
    def name_differs_only_by_case(self) -> bool:
        return (
            self.internal_name is not None
            and self.internal_name != self.name
            and self.internal_name.lower() == self.name.lower()
        )


@dataclass(frozen=True)
class Footprint:
    category: str
    name: str
    path: Path
    internal_name: Optional[str] = None
    model_paths: Tuple[str, ...] = ()      # raw, exactly as written in the file
    error: Optional[str] = None

    @property
    def lib_id(self) -> str:
        return f"{self.category}:{self.name}"

    @property
    def name_matches_internal(self) -> bool:
        return self.internal_name is None or self.internal_name == self.name


@dataclass(frozen=True)
class Model:
    category: str
    filename: str
    path: Path

    @property
    def rel_uri(self) -> str:
        """The canonical ${KICAD_CUSTOM_LIB} path a footprint should point at."""
        return f"{LIB_VAR_PREFIX}/3dmodels/{self.category}{SHAPES_SUFFIX}/{self.filename}"


# --------------------------------------------------------------------------
# Model path classification
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelPathInfo:
    raw: str
    kind: str                      # custom_lib | absolute | other_var | relative
    resolved: Optional[Path] = None
    filename: str = ""

    @property
    def is_canonical(self) -> bool:
        return self.kind == "custom_lib"


def classify_model_path(root: Path, raw: str) -> ModelPathInfo:
    """
    Work out what a (model "...") path refers to.

    Only ${KICAD_CUSTOM_LIB}-relative paths are acceptable in this library
    (AGENTS.md Rule 2); everything else is reported so `check` can flag it.
    """
    norm = raw.replace("\\", "/")
    filename = norm.rsplit("/", 1)[-1]

    if norm.startswith(LIB_VAR_PREFIX):
        rel = norm[len(LIB_VAR_PREFIX):].lstrip("/")
        return ModelPathInfo(raw, "custom_lib", (root / rel), filename)

    if norm.startswith("${") or norm.startswith("$("):
        return ModelPathInfo(raw, "other_var", None, filename)

    # A drive letter or a leading slash is an absolute path.
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
        return ModelPathInfo(raw, "absolute", Path(raw), filename)

    return ModelPathInfo(raw, "relative", None, filename)


# --------------------------------------------------------------------------
# Resolved cross-references
# --------------------------------------------------------------------------

@dataclass
class DanglingRef:
    """A reference that does not resolve on this disk."""
    kind: str          # symbol_footprint | footprint_model | symbol_extends
    source: str        # lib_id or repo-relative path of the referrer
    target: str        # what it pointed at
    detail: str = ""


@dataclass
class Refs:
    symbol_to_footprint: Dict[str, str] = field(default_factory=dict)
    footprint_to_models: Dict[str, List[str]] = field(default_factory=dict)
    footprint_users: Dict[str, List[str]] = field(default_factory=dict)
    model_users: Dict[str, List[str]] = field(default_factory=dict)
    dangling: List[DanglingRef] = field(default_factory=list)
    unlinked_symbols: List[str] = field(default_factory=list)
    orphan_footprints: List[str] = field(default_factory=list)
    orphan_models: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------

@dataclass
class Library:
    root: Path
    categories: Dict[str, Category] = field(default_factory=dict)
    symbols: List[Symbol] = field(default_factory=list)
    footprints: List[Footprint] = field(default_factory=list)
    models: List[Model] = field(default_factory=list)
    unreadable: List[Tuple[Path, str]] = field(default_factory=list)

    # -- lookups ----------------------------------------------------------
    def category_names(self) -> List[str]:
        return sorted(self.categories, key=_sort_key)

    def non_empty_categories(self) -> List[str]:
        return [n for n in self.category_names() if not self.categories[n].is_empty]

    def symbols_in(self, category: str) -> List[Symbol]:
        return [s for s in self.symbols if s.category == category]

    def footprints_in(self, category: str) -> List[Footprint]:
        return [f for f in self.footprints if f.category == category]

    def models_in(self, category: str) -> List[Model]:
        return [m for m in self.models if m.category == category]

    def find_symbol(self, category: str, name: str) -> Optional[Symbol]:
        for s in self.symbols:
            if s.category == category and s.name == name:
                return s
        return None

    def find_footprint(self, category: str, name: str) -> Optional[Footprint]:
        for f in self.footprints:
            if f.category == category and f.name == name:
                return f
        return None

    def footprint_by_lib_id(self, lib_id: str) -> Optional[Footprint]:
        if ":" not in lib_id:
            return None
        cat, name = lib_id.split(":", 1)
        return self.find_footprint(cat, name)

    def find_model(self, category: str, filename: str) -> Optional[Model]:
        for m in self.models:
            if m.category == category and m.filename == filename:
                return m
        return None

    def rel(self, path: Path) -> str:
        """Repo-relative POSIX path, for messages that must look the same on
        both machines."""
        try:
            return Path(path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return Path(path).as_posix()

    # -- resolution -------------------------------------------------------
    def resolve(self) -> Refs:
        """
        Walk every reference and record what resolves and what does not.

        Run after scan(); pure computation over the already-read index.
        """
        refs = Refs()
        fp_ids = {f.lib_id for f in self.footprints}

        for sym in self.symbols:
            if sym.extends:
                sibling = sym.path.parent / f"{sym.extends}{SYMBOL_EXT}"
                if not sibling.exists():
                    refs.dangling.append(DanglingRef(
                        "symbol_extends", sym.lib_id, sym.extends,
                        f"derived symbol's parent '{sym.extends}' is not a file in "
                        f"{self.rel(sym.path.parent)}",
                    ))

            ref = (sym.footprint_ref or "").strip()
            if not ref:
                refs.unlinked_symbols.append(sym.lib_id)
                continue
            refs.symbol_to_footprint[sym.lib_id] = ref
            if ref in fp_ids:
                refs.footprint_users.setdefault(ref, []).append(sym.lib_id)
            else:
                refs.dangling.append(DanglingRef(
                    "symbol_footprint", sym.lib_id, ref,
                    "Footprint property points at a footprint that is not in this library",
                ))

        for fp in self.footprints:
            resolved_for_fp: List[str] = []
            for raw in fp.model_paths:
                info = classify_model_path(self.root, raw)
                if not info.is_canonical:
                    refs.dangling.append(DanglingRef(
                        "footprint_model", fp.lib_id, raw,
                        f"model path is {info.kind}, not {LIB_VAR_PREFIX}-relative",
                    ))
                    continue
                if info.resolved is None or not info.resolved.exists():
                    refs.dangling.append(DanglingRef(
                        "footprint_model", fp.lib_id, raw,
                        "model file does not exist on disk",
                    ))
                    continue
                key = self.rel(info.resolved)
                resolved_for_fp.append(key)
                refs.model_users.setdefault(key, []).append(fp.lib_id)
            refs.footprint_to_models[fp.lib_id] = resolved_for_fp

            if fp.lib_id not in refs.footprint_users:
                refs.orphan_footprints.append(fp.lib_id)

        for m in self.models:
            if self.rel(m.path) not in refs.model_users:
                refs.orphan_models.append(self.rel(m.path))

        refs.unlinked_symbols.sort(key=_sort_key)
        refs.orphan_footprints.sort(key=_sort_key)
        refs.orphan_models.sort(key=_sort_key)
        return refs


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def _read_symbol(path: Path, category: str) -> Symbol:
    try:
        text = sx.read_text(path)
    except OSError as exc:
        return Symbol(category, path.stem, path, error=f"cannot read: {exc}")

    try:
        tops = sx.top_level_symbols(text)
    except sx.SExprError as exc:
        return Symbol(category, path.stem, path, error=f"malformed S-expression: {exc}")

    if not tops:
        return Symbol(category, path.stem, path, error="file declares no symbol")

    name, open_idx, _close = tops[0]
    extends = sx.extends_target(text, open_idx)
    return Symbol(
        category=category,
        name=path.stem,
        path=path,
        internal_name=name,
        footprint_ref=sx.get_property(text, open_idx, "Footprint"),
        extends=extends[0] if extends else None,
        extra_symbols=tuple(n for n, _o, _c in tops[1:]),
    )


def _read_footprint(path: Path, category: str) -> Footprint:
    try:
        text = sx.read_text(path)
    except OSError as exc:
        return Footprint(category, path.stem, path, error=f"cannot read: {exc}")

    try:
        name, _open, _close = sx.footprint_span(text)
        models = sx.all_models(text)
    except sx.SExprError as exc:
        return Footprint(category, path.stem, path, error=f"malformed S-expression: {exc}")

    return Footprint(
        category=category,
        name=path.stem,
        path=path,
        internal_name=name,
        model_paths=tuple(m.path for m in models),
    )


def _categories_from_dirs(root: Path) -> Dict[str, Dict[str, bool]]:
    found: Dict[str, Dict[str, bool]] = {}
    for sub, suffix, key in (
        ("symbols", SYMDIR_SUFFIX, "sym"),
        ("footprints", PRETTY_SUFFIX, "fp"),
        ("3dmodels", SHAPES_SUFFIX, "model"),
    ):
        d = root / sub
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_dir() and p.name.endswith(suffix):
                found.setdefault(p.name[: -len(suffix)], {})[key] = True
    return found


def scan(root: Path) -> Library:
    """
    Build the index. Read-only; safe to call as often as the UI likes.

    Ordering is fully deterministic so generated tables and dialog lists are
    identical on both machines regardless of directory iteration order.
    """
    root = Path(root).resolve()
    lib = Library(root=root)
    dirs = _categories_from_dirs(root)

    for cat in sorted(dirs, key=_sort_key):
        flags = dirs[cat]
        sym_dir = root / "symbols" / f"{cat}{SYMDIR_SUFFIX}"
        fp_dir = root / "footprints" / f"{cat}{PRETTY_SUFFIX}"
        model_dir = root / "3dmodels" / f"{cat}{SHAPES_SUFFIX}"

        symbols: List[Symbol] = []
        if flags.get("sym"):
            for p in sorted(sym_dir.glob(f"*{SYMBOL_EXT}"), key=lambda q: _sort_key(q.name)):
                sym = _read_symbol(p, cat)
                if sym.error:
                    lib.unreadable.append((p, sym.error))
                symbols.append(sym)

        footprints: List[Footprint] = []
        if flags.get("fp"):
            for p in sorted(fp_dir.glob(f"*{FOOTPRINT_EXT}"), key=lambda q: _sort_key(q.name)):
                fp = _read_footprint(p, cat)
                if fp.error:
                    lib.unreadable.append((p, fp.error))
                footprints.append(fp)

        models: List[Model] = []
        if flags.get("model"):
            for p in sorted(model_dir.iterdir(), key=lambda q: _sort_key(q.name)):
                if p.is_file() and is_model_file(p):
                    models.append(Model(cat, p.name, p))

        lib.symbols.extend(symbols)
        lib.footprints.extend(footprints)
        lib.models.extend(models)
        lib.categories[cat] = Category(
            name=cat,
            has_symbol_dir=bool(flags.get("sym")),
            has_footprint_dir=bool(flags.get("fp")),
            has_model_dir=bool(flags.get("model")),
            symbol_count=len(symbols),
            footprint_count=len(footprints),
            model_count=len(models),
        )

    return lib
