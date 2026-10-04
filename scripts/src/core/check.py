"""
Full library audit.

Returns structured findings with a severity so callers can decide what to do:
the CLI exits non-zero when any `error` is present (the old `check` always
exited 0, so it could never fail a CI run), and the GUI can filter.

The old audit only looked at files the manifest listed, which meant anything
the manifest had forgotten about was invisible -- and since the manifest was
also the thing most likely to be wrong, that was exactly backwards. This
audit starts from the disk.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from . import library as lb
from . import naming
from . import provenance as pv
from . import table_gen as tg

ERROR = "error"
WARNING = "warning"
INFO = "info"

_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    where: str = ""          # repo-relative path or lib_id
    remedy: str = ""

    def format(self) -> str:
        head = f"[{self.severity}] {self.code}"
        if self.where:
            head += f" ({self.where})"
        out = f"{head}: {self.message}"
        if self.remedy:
            out += f"\n    -> {self.remedy}"
        return out

    def to_json(self) -> Dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "where": self.where,
            "remedy": self.remedy,
        }


@dataclass
class Report:
    root: Path
    findings: List[Finding] = field(default_factory=list)
    kicad_cli_used: bool = False

    def add(self, severity: str, code: str, message: str, where: str = "", remedy: str = "") -> None:
        self.findings.append(Finding(severity, code, message, where, remedy))

    def of(self, severity: str) -> List[Finding]:
        return [f for f in self.findings if f.severity == severity]

    @property
    def errors(self) -> List[Finding]:
        return self.of(ERROR)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def exit_code(self) -> int:
        """Non-zero when anything is actually broken, so CI can gate on it."""
        return 1 if self.has_errors else 0

    def sorted(self) -> List[Finding]:
        return sorted(self.findings, key=lambda f: (_ORDER[f.severity], f.code, f.where))

    def counts(self) -> Dict[str, int]:
        return {s: len(self.of(s)) for s in (ERROR, WARNING, INFO)}

    def summary(self) -> str:
        c = self.counts()
        lines = [
            f"Library audit: {c[ERROR]} error(s), {c[WARNING]} warning(s), "
            f"{c[INFO]} note(s)"
        ]
        if not self.findings:
            lines.append("Nothing to report.")
        for f in self.sorted():
            lines.append(f.format())
        if not self.kicad_cli_used:
            lines.append(
                "note: the kicad-cli parse checks did not run (not installed, or "
                "skipped), so the files were not validated by KiCad itself"
            )
        return "\n".join(lines)

    def to_json(self) -> Dict[str, object]:
        return {
            "root": str(self.root),
            "counts": self.counts(),
            "kicad_cli_used": self.kicad_cli_used,
            "findings": [f.to_json() for f in self.sorted()],
        }


# --------------------------------------------------------------------------
# kicad-cli parse checks
# --------------------------------------------------------------------------

def _last_line(proc: subprocess.CompletedProcess) -> str:
    """The most informative line of a kicad-cli failure."""
    text = (proc.stderr or proc.stdout or "").strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[-1] if lines else "no detail reported"


def _check_footprint_libs(report: Report, lib: lb.Library, cli: str) -> None:
    """
    `fp export svg` returns a non-zero exit code on a corrupt footprint, so
    the exit code alone is a trustworthy signal (AGENTS.md 6.4).
    """
    for cat in lib.category_names():
        if lib.categories[cat].footprint_count == 0:
            continue
        pretty = lib.root / "footprints" / f"{cat}{lb.PRETTY_SUFFIX}"
        with tempfile.TemporaryDirectory(prefix="kicad_check_") as tmp:
            out = Path(tmp) / "svg"
            out.mkdir()  # kicad-cli will not create it, and silently fails if absent
            proc = subprocess.run(
                [cli, "fp", "export", "svg", "-o", str(out), str(pretty)],
                capture_output=True, text=True,
            )
        if proc.returncode != 0:
            report.add(
                ERROR, "footprint-unparseable",
                f"KiCad could not load this footprint library: {_last_line(proc)}",
                where=lib.rel(pretty),
                remedy="open the library in the Footprint Editor to find the bad file",
            )


def _check_symbol_libs(report: Report, lib: lb.Library, cli: str) -> None:
    """
    `sym export svg` exits 0 even on a corrupt library, so it cannot be used
    as a validator. `sym upgrade` does report failure, but rewrites files in
    place -- so it is run against a throwaway copy (AGENTS.md 6.4).
    """
    for cat in lib.category_names():
        if lib.categories[cat].symbol_count == 0:
            continue
        symdir = lib.root / "symbols" / f"{cat}{lb.SYMDIR_SUFFIX}"
        with tempfile.TemporaryDirectory(prefix="kicad_check_") as tmp:
            copy = Path(tmp) / symdir.name
            shutil.copytree(symdir, copy)
            proc = subprocess.run(
                [cli, "sym", "upgrade", str(copy)], capture_output=True, text=True
            )
        if proc.returncode != 0:
            report.add(
                ERROR, "symbol-unparseable",
                f"KiCad could not load this symbol library: {_last_line(proc)}",
                where=lib.rel(symdir),
                remedy="open the library in the Symbol Editor to find the bad file",
            )


# --------------------------------------------------------------------------
# The audit
# --------------------------------------------------------------------------

def run(
    root: Path,
    *,
    use_kicad_cli: bool = True,
    lib: Optional[lb.Library] = None,
) -> Report:
    """Audit the library. Read-only; never modifies anything."""
    root = Path(root).resolve()
    lib = lib if lib is not None else lb.scan(root)
    report = Report(root=root)
    refs = lib.resolve()

    # --- unreadable files ------------------------------------------------
    for path, error in lib.unreadable:
        report.add(
            ERROR, "file-unreadable", error, where=lib.rel(path),
            remedy="restore the file from git, or re-import it from the vendor bundle",
        )

    # --- generated tables ------------------------------------------------
    diffs = tg.diff_tables(root, lib)
    for filename, diff in diffs.items():
        if not diff:
            continue
        report.add(
            ERROR, "table-stale",
            f"{filename} does not match the directory contents",
            where=filename,
            remedy="run: python scripts/lib_manager.py generate",
        )

    # --- categories ------------------------------------------------------
    for cat_name in lib.category_names():
        cat = lib.categories[cat_name]
        if cat.is_empty:
            report.add(
                WARNING, "category-empty",
                f"category '{cat_name}' contains no symbols, footprints or 3D "
                f"models; git does not track empty directories, so it will not "
                f"exist on another machine",
                where=cat_name,
                remedy=f"import something into '{cat_name}', or delete its three "
                       f"directories and run generate",
            )
            continue
        missing = cat.missing_mirrors
        if missing:
            report.add(
                INFO, "category-partial",
                f"category '{cat_name}' has no {', '.join(missing)}",
                where=cat_name,
                remedy="normal for a symbol-only or footprint-only library; the "
                       "directories are created when something is placed in them",
            )

    for group in naming.find_case_collisions(lib.category_names()):
        report.add(
            ERROR, "category-case-collision",
            f"categories {' and '.join(group)} differ only by case and are the "
            f"same directory on Windows and macOS",
            remedy="rename one of them",
        )

    for cat_name in lib.category_names():
        official = naming.official_collision(cat_name)
        if official:
            report.add(
                WARNING, "category-shadows-official",
                f"category '{cat_name}' has the same nickname as the official "
                f"KiCad library '{official}'; which one a lib_id resolves to "
                f"depends on global table order and can differ between machines",
                where=cat_name,
                remedy=f"rename it, e.g. to '{naming.suggest_prefixed(cat_name)}'",
            )

    # --- names -----------------------------------------------------------
    for cat_name in lib.category_names():
        for kind, items in (
            ("symbol", [s.name for s in lib.symbols_in(cat_name)]),
            ("footprint", [f.name for f in lib.footprints_in(cat_name)]),
            ("model", [m.filename for m in lib.models_in(cat_name)]),
        ):
            for group in naming.find_case_collisions(items):
                report.add(
                    ERROR, f"{kind}-case-collision",
                    f"{kind}s {' and '.join(group)} in '{cat_name}' differ only by "
                    f"case and cannot coexist on Windows or macOS",
                    where=cat_name,
                    remedy="rename one of them",
                )

    for sym in lib.symbols:
        if sym.error:
            continue
        if not naming.is_valid(sym.name):
            report.add(
                WARNING, "symbol-name-invalid",
                f"'{sym.name}' contains characters that are awkward in a lib_id "
                f"or a filename",
                where=sym.lib_id,
                remedy=f"rename it to '{naming.sanitize(sym.name)}'",
            )
        if sym.extra_symbols:
            report.add(
                WARNING, "symbol-file-multi",
                f"this file declares {1 + len(sym.extra_symbols)} top-level "
                f"symbols ({', '.join((sym.internal_name or '?',) + sym.extra_symbols)}); "
                f"every one of the 22784 official symbol files declares exactly one",
                where=lib.rel(sym.path),
                remedy="re-import the file so it is split into one symbol per file",
            )
        elif sym.name_differs_only_by_case:
            report.add(
                INFO, "symbol-name-case",
                f"filename stem '{sym.name}' and internal name "
                f"'{sym.internal_name}' differ only by case; KiCad's own "
                f"Interface_USB/tusb564.kicad_sym does the same",
                where=sym.lib_id,
            )
        elif not sym.name_matches_internal:
            report.add(
                WARNING, "symbol-name-mismatch",
                f"filename stem '{sym.name}' does not match the internal symbol "
                f"name '{sym.internal_name}'",
                where=sym.lib_id,
                remedy=f"rename the symbol so both agree",
            )

    for fp in lib.footprints:
        if fp.error:
            continue
        if not fp.name_matches_internal:
            report.add(
                WARNING, "footprint-name-mismatch",
                f"filename stem '{fp.name}' does not match the internal footprint "
                f"name '{fp.internal_name}'; KiCad resolves a lib_id by filename, "
                f"so the two must agree",
                where=fp.lib_id,
                remedy="rename the footprint so both agree",
            )

    # --- references ------------------------------------------------------
    for d in refs.dangling:
        if d.kind == "symbol_footprint":
            report.add(
                ERROR, "footprint-ref-broken",
                f"Footprint property points at '{d.target}', which is not in this "
                f"library",
                where=d.source,
                remedy="import the missing footprint, or point the symbol at an "
                       "existing one",
            )
        elif d.kind == "footprint_model":
            if "absolute" in d.detail or "relative" in d.detail or "other_var" in d.detail:
                report.add(
                    ERROR, "model-path-not-portable",
                    f"model path {d.target!r} is not ${{KICAD_CUSTOM_LIB}}-relative, "
                    f"so it will not resolve on another machine",
                    where=d.source,
                    remedy="re-import the footprint, or fix the path in the "
                           "Footprint Editor's 3D Models tab",
                )
            else:
                report.add(
                    ERROR, "model-file-missing",
                    f"model path {d.target!r} does not exist on disk",
                    where=d.source,
                    remedy="import the model file, or remove the model block",
                )
        elif d.kind == "symbol_extends":
            report.add(
                ERROR, "extends-parent-missing",
                f"derived symbol extends '{d.target}', but there is no "
                f"{d.target}.kicad_sym in the same symdir; KiCad cannot load it",
                where=d.source,
                remedy=f"import or move '{d.target}' into the same category",
            )

    for lib_id in refs.unlinked_symbols:
        report.add(
            INFO, "symbol-no-footprint",
            "symbol has no Footprint property set",
            where=lib_id,
            remedy="fine for a schematic-only symbol; otherwise assign a footprint",
        )

    for lib_id in refs.orphan_footprints:
        report.add(
            INFO, "footprint-orphan",
            "no symbol in this library references this footprint",
            where=lib_id,
        )

    for rel_path in refs.orphan_models:
        report.add(
            INFO, "model-orphan",
            "no footprint references this 3D model",
            where=rel_path,
        )

    # --- provenance ------------------------------------------------------
    try:
        prov = pv.load(root)
    except pv.ProvenanceError as exc:
        report.add(
            ERROR, "provenance-corrupt", str(exc), where=pv.FILENAME,
            remedy="fix or delete the file, or restore it from git",
        )
    else:
        for key in pv.stale_keys(prov, lib):
            cat, kind, name = pv.split_key(key)
            report.add(
                WARNING, "provenance-stale",
                f"provenance records {kind} '{name}' in '{cat}', but nothing "
                f"on disk matches",
                where=key,
                # Nothing removes these automatically -- save() writes items
                # verbatim -- so point at the command that does.
                remedy="the item was deleted or renamed outside the tool; run "
                       "'python scripts/lib_manager.py prune-provenance' to drop it",
            )

    # --- KiCad parse checks ----------------------------------------------
    cli = shutil.which("kicad-cli") if use_kicad_cli else None
    if cli:
        report.kicad_cli_used = True
        _check_symbol_libs(report, lib, cli)
        _check_footprint_libs(report, lib, cli)

    return report
