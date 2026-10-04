"""
Core library management modules.

The disk is the source of truth: `library.scan()` builds the index, and every
mutating action builds an `ops.Plan` that can be previewed before `ops.apply()`
runs it.

  naming      name validation, sanitising, official-nickname collisions
  s_expr      quote- and paren-aware KiCad S-expression read/edit/write
  library     read-only scanner -> in-memory index and reference resolution
  provenance  provenance.json, plus migration from the old manifest.json
  ops         Plan / Operation / apply, conflict policies, atomic writes
  ingest      candidate detection, auto-pairing, import planning
  refactor    rename and move with library-wide reference rewriting
  table_gen   sym-lib-table / fp-lib-table generation and staleness
  check       structured audit with severities
  packager    lean per-project export
"""

from . import (
    check,
    ingest,
    library,
    naming,
    ops,
    packager,
    provenance,
    refactor,
    s_expr,
    table_gen,
)
from .library import Library, scan
from .ops import ConflictPolicy, Plan, apply
from .packager import ProjectPackager
from .table_gen import generate_tables, scan_libraries

__all__ = [
    # modules
    "check", "ingest", "library", "naming", "ops", "packager", "provenance",
    "refactor", "s_expr", "table_gen",
    # common entry points
    "Library", "scan", "Plan", "apply", "ConflictPolicy",
    "generate_tables", "scan_libraries", "ProjectPackager",
]
