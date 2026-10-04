"""
Generation of the master sym-lib-table and fp-lib-table.

Only libraries that actually contain a file are listed. The previous version
listed every directory it found, which is how a stale, empty '3255' category
ended up advertised in both committed tables: KiCad then shows an empty
library, and because git does not track empty directories the other machine
gets a table entry pointing at a folder that does not exist there at all.

Output is deterministic -- sorted, no timestamps -- so the tables only change
in git when the library's contents actually change.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import library as lb
from . import naming
from . import ops
from . import s_expr as sx

TABLE_VERSION = 7

SYM_TABLE_NAME = "sym-lib-table"
FP_TABLE_NAME = "fp-lib-table"


@dataclass(frozen=True)
class LibEntry:
    """One (lib ...) row."""
    name: str
    uri: str
    descr: str

    def render(self) -> str:
        return (
            f'\t(lib (name {sx.quote(self.name)})'
            f' (type "KiCad")'
            f' (uri {sx.quote(self.uri)})'
            f' (options "")'
            f' (descr {sx.quote(self.descr)}))'
        )


def _render_table(root_tag: str, entries: List[LibEntry]) -> str:
    lines = [f"({root_tag}", f"\t(version {TABLE_VERSION})"]
    lines += [e.render() for e in entries]
    lines.append(")")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Entry collection
# --------------------------------------------------------------------------

def symbol_entries(lib: lb.Library) -> List[LibEntry]:
    """Symbol libraries holding at least one .kicad_sym."""
    return [
        LibEntry(
            name=cat,
            uri=f"{lb.LIB_VAR_PREFIX}/symbols/{cat}{lb.SYMDIR_SUFFIX}",
            descr=f"Custom symbol library {cat}",
        )
        for cat in lib.category_names()
        if lib.categories[cat].symbol_count > 0
    ]


def footprint_entries(lib: lb.Library) -> List[LibEntry]:
    """Footprint libraries holding at least one .kicad_mod."""
    return [
        LibEntry(
            name=cat,
            uri=f"{lb.LIB_VAR_PREFIX}/footprints/{cat}{lb.PRETTY_SUFFIX}",
            descr=f"Custom footprint library {cat}",
        )
        for cat in lib.category_names()
        if lib.categories[cat].footprint_count > 0
    ]


def render_tables(lib: lb.Library) -> Dict[str, str]:
    """{filename: content} for both master tables."""
    return {
        SYM_TABLE_NAME: _render_table("sym_lib_table", symbol_entries(lib)),
        FP_TABLE_NAME: _render_table("fp_lib_table", footprint_entries(lib)),
    }


def skipped_empty_categories(lib: lb.Library) -> List[str]:
    """Categories deliberately left out because they hold no files at all."""
    return [c for c in lib.category_names() if lib.categories[c].is_empty]


# --------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------

def _current_text(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        return sx.read_text(path)
    except OSError:
        return None


def diff_tables(root: Path, lib: Optional[lb.Library] = None) -> Dict[str, List[str]]:
    """
    Unified diff per table, between what is on disk and what would be
    generated. An empty list means that table is up to date.
    """
    root = Path(root).resolve()
    lib = lib if lib is not None else lb.scan(root)
    out: Dict[str, List[str]] = {}
    for filename, wanted in render_tables(lib).items():
        path = root / filename
        current = _current_text(path)
        if current == wanted:
            out[filename] = []
            continue
        out[filename] = list(difflib.unified_diff(
            (current or "").splitlines(keepends=True),
            wanted.splitlines(keepends=True),
            fromfile=f"{filename} (on disk)",
            tofile=f"{filename} (generated)",
        ))
    return out


def is_stale(root: Path, lib: Optional[lb.Library] = None) -> bool:
    """True when either table on disk disagrees with the library contents."""
    return any(diff_tables(root, lib).values())


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def plan_generate(root: Path, lib: Optional[lb.Library] = None) -> ops.Plan:
    """
    A plan that rewrites whichever tables are out of date.

    Tables already matching the library are left alone, so regenerating is
    free and never dirties the working tree.
    """
    root = Path(root).resolve()
    lib = lib if lib is not None else lb.scan(root)
    plan = ops.Plan(root=root, title="Regenerate master library tables")

    for filename, wanted in render_tables(lib).items():
        path = root / filename
        if _current_text(path) == wanted:
            continue
        plan.write(path, wanted, note="generated from the directory contents")

    for cat in skipped_empty_categories(lib):
        plan.note(
            f"category '{cat}' holds no symbols, footprints or 3D models and is "
            f"omitted from both tables"
        )

    for cat in lib.category_names():
        official = naming.official_collision(cat)
        if official:
            plan.warn(
                f"category '{cat}' collides with the official KiCad library "
                f"'{official}'; lib_id resolution depends on global table order"
            )

    for group in naming.find_case_collisions(lib.category_names()):
        plan.warn(
            f"categories {' and '.join(group)} differ only by case and are the "
            f"same directory on Windows and macOS"
        )

    return plan


# --------------------------------------------------------------------------
# Convenience
# --------------------------------------------------------------------------

def generate_tables(root_dir: Path) -> Tuple[Path, Path]:
    """
    Write both tables, returning their paths.

    Kept for the existing CLI and GUI call sites; new code should build the
    plan so the change can be previewed first.
    """
    root = Path(root_dir).resolve()
    plan = plan_generate(root)
    result = ops.apply(plan)
    if not result.ok:
        op, err = result.failed
        raise ops.ApplyError(f"could not write {op.target}: {err}")
    return root / SYM_TABLE_NAME, root / FP_TABLE_NAME


def scan_libraries(root_dir: Path) -> Dict[str, List[Tuple[str, Path, str]]]:
    """
    Deprecated shim mirroring the old return shape, now restricted to
    non-empty libraries.
    """
    root = Path(root_dir).resolve()
    lib = lb.scan(root)
    return {
        "symbols": [
            (e.name, root / "symbols" / f"{e.name}{lb.SYMDIR_SUFFIX}", e.uri)
            for e in symbol_entries(lib)
        ],
        "footprints": [
            (e.name, root / "footprints" / f"{e.name}{lb.PRETTY_SUFFIX}", e.uri)
            for e in footprint_entries(lib)
        ],
    }
