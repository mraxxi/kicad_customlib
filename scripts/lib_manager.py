#!/usr/bin/env python3
"""
KiCad Custom Library Manager (KICAD_CUSTOM_LIB).

Every mutating command builds a plan, shows it, and asks before touching the
disk. `--dry-run` stops after showing it; `--yes` skips the question. After a
successful change the master tables are regenerated and provenance is saved,
so the library is never left in a state where the tables disagree with the
directories.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import traceback
from pathlib import Path
from typing import List, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from src.core import check as check_mod            # noqa: E402
from src.core import ingest as ingest_mod          # noqa: E402
from src.core import library as lb                 # noqa: E402
from src.core import naming                        # noqa: E402
from src.core import ops                           # noqa: E402
from src.core import packager as packager_mod      # noqa: E402
from src.core import provenance as pv              # noqa: E402
from src.core import refactor as rf                # noqa: E402
from src.core import table_gen as tg               # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ABORTED = 2


class UsageError(ValueError):
    """The command line is well-formed but asks for something impossible."""

TK_HINT = {
    "arch": "sudo pacman -S tk",
    "debian": "sudo apt install python3-tk",
    "fedora": "sudo dnf install python3-tkinter",
    "macos": "brew install python-tk",
}


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

def _out(message: str = "") -> None:
    print(message)


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _confirm(prompt: str, assume_yes: bool) -> bool:
    """
    Ask before applying. Non-interactive runs must pass --yes explicitly, so
    a script or CI job can never silently mutate the library.
    """
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        _err("Refusing to apply without confirmation: stdin is not a terminal. "
             "Re-run with --yes, or with --dry-run to see the plan only.")
        return False
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        _out()
        return False
    return answer in ("y", "yes")


def _run_plan(
    plan: ops.Plan,
    args: argparse.Namespace,
    *,
    prov: Optional[pv.Provenance] = None,
    regenerate: bool = True,
) -> int:
    """
    Show a plan, confirm it, apply it, then bring the tables and provenance
    back in step.
    """
    _out(plan.summary())

    if plan.is_empty:
        return EXIT_OK
    if getattr(args, "dry_run", False):
        _out("\n(dry run: nothing was changed)")
        return EXIT_OK
    if not _confirm("\nApply this plan?", getattr(args, "yes", False)):
        _out("Aborted.")
        return EXIT_ABORTED

    result = ops.apply(plan)
    _out(result.summary(plan.root))
    if not result.ok:
        return EXIT_ERROR

    # Deferred provenance edits run only now, after the operations succeeded.
    if prov is not None:
        changed = pv.apply_edits(prov, plan.provenance)
        if changed or prov.items or prov.existed:
            prov.save()
            _out(f"updated {prov.path.name}")

    if regenerate:
        follow_up = tg.plan_generate(ROOT_DIR)
        if not follow_up.is_empty:
            table_result = ops.apply(follow_up)
            if not table_result.ok:
                _err("tables could not be regenerated; run 'generate' manually")
                return EXIT_ERROR
            for op in follow_up.operations:
                _out(f"regenerated {op.target.name}")

    return EXIT_OK


def _split_lib_id(value: str, category: Optional[str], kind: str) -> tuple[str, str]:
    """Accept either 'Category:Name' or a bare name plus --category."""
    if ":" in value:
        cat, name = value.split(":", 1)
        if category and category != cat:
            raise UsageError(
                f"conflicting categories: '{value}' says '{cat}' but --category "
                f"says '{category}'"
            )
        return cat, name
    if not category:
        raise UsageError(
            f"which category is this {kind} in? pass --category CATEGORY, or "
            f"write it as 'Category:{value}'"
        )
    return category, value


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_generate(args: argparse.Namespace) -> int:
    plan = tg.plan_generate(ROOT_DIR)
    if plan.is_empty:
        _out("Tables are already up to date.")
        for note in plan.notes:
            _out(f"  note: {note}")
        return EXIT_OK
    return _run_plan(plan, args, regenerate=False)


def cmd_check(args: argparse.Namespace) -> int:
    report = check_mod.run(ROOT_DIR, use_kicad_cli=not args.no_kicad_cli)
    if args.json:
        _out(json.dumps(report.to_json(), indent=2))
    else:
        _out(report.summary())
    return report.exit_code


def cmd_ingest(args: argparse.Namespace) -> int:
    prov = pv.load(ROOT_DIR)
    select = None
    if args.select and args.select != ["all"]:
        select = args.select

    with ingest_mod.prepare(
        ROOT_DIR, Path(args.source), args.category,
        select=select,
        conflict=ops.ConflictPolicy(args.conflict),
    ) as preview:
        _out(f"Detected {len(preview.candidates)} candidate(s) in {args.source}:")
        for candidate in preview.candidates:
            mark = " " if candidate.include else "-"
            _out(f"  {mark} {candidate.describe()}")
        _out()
        return _run_plan(preview.plan, args, prov=prov)


def cmd_sync_staging(args: argparse.Namespace) -> int:
    """
    Import everything under staging-temp/intake/<Category>/.

    Each item is independent: one failure is reported and the rest continue,
    where the old implementation aborted the whole batch. With --archive a
    successfully imported item is moved into staging-temp/imported_archive/
    so the next run does not redo it.
    """
    intake = ROOT_DIR / "staging-temp" / "intake"
    archive = ROOT_DIR / "staging-temp" / "imported_archive"

    if not intake.is_dir():
        _out(f"No intake directory at {intake.relative_to(ROOT_DIR)}.")
        _out("Create it and drop <Category>/<part> folders or ZIPs inside.")
        return EXIT_OK

    jobs: List[tuple[str, Path]] = []
    for category_dir in sorted(p for p in intake.iterdir() if p.is_dir()):
        for item in sorted(category_dir.iterdir()):
            if item.is_dir() or item.suffix.lower() == ".zip":
                jobs.append((category_dir.name, item))

    if not jobs:
        _out(f"Nothing to import in {intake.relative_to(ROOT_DIR)}.")
        return EXIT_OK

    _out(f"Found {len(jobs)} item(s) to import.\n")
    prov = pv.load(ROOT_DIR)
    succeeded: List[Path] = []
    failed: List[tuple[Path, str]] = []

    for category, item in jobs:
        label = f"{category}/{item.name}"
        _out(f"=== {label} ===")
        try:
            with ingest_mod.prepare(
                ROOT_DIR, item, category,
                conflict=ops.ConflictPolicy(args.conflict),
            ) as preview:
                _out(preview.plan.summary())
                if preview.plan.is_empty:
                    # Nothing importable: a stray file, or an archive that is
                    # not really an archive. Counted as a failure so the run
                    # does not claim to have imported it.
                    failed.append((item, "nothing importable found"))
                    _out()
                    continue
                if args.dry_run:
                    _out("(dry run)")
                    continue
                if not _confirm(f"Apply the plan for {label}?", args.yes):
                    _out("skipped")
                    continue
                result = ops.apply(preview.plan)
                _out(result.summary(ROOT_DIR))
                if result.ok:
                    succeeded.append(item)
                else:
                    failed.append((item, "apply failed"))
        except Exception as exc:  # noqa: BLE001 -- one bad item must not stop the batch
            failed.append((item, f"{type(exc).__name__}: {exc}"))
            _err(f"failed: {exc}")
            if args.debug:
                traceback.print_exc()
        _out()

    if not args.dry_run:
        prov.save()
        table_plan = tg.plan_generate(ROOT_DIR)
        if not table_plan.is_empty:
            ops.apply(table_plan)
            _out("regenerated master tables")

    if args.archive and succeeded:
        archive.mkdir(parents=True, exist_ok=True)
        for item in succeeded:
            destination = archive / item.name
            if destination.exists():
                destination = ops.unique_target(destination)
            shutil.move(str(item), str(destination))
            _out(f"archived {item.name} -> {destination.relative_to(ROOT_DIR)}")

    _out(f"\nImported {len(succeeded)} item(s); {len(failed)} failed.")
    for item, reason in failed:
        _out(f"  ! {item.name}: {reason}")
    return EXIT_ERROR if failed else EXIT_OK


def cmd_rename(args: argparse.Namespace) -> int:
    prov = pv.load(ROOT_DIR)
    kind = args.kind

    if kind == rf.KIND_CATEGORY:
        plan = rf.plan_rename_category(
            ROOT_DIR, args.old, args.new,
            conflict=ops.ConflictPolicy(args.conflict),
        )
    else:
        category, old = _split_lib_id(args.old, args.category, kind)
        new = args.new.split(":", 1)[-1]
        common = dict(conflict=ops.ConflictPolicy(args.conflict))
        if kind == rf.KIND_SYMBOL:
            plan = rf.plan_rename_symbol(ROOT_DIR, category, old, new, **common)
        elif kind == rf.KIND_FOOTPRINT:
            plan = rf.plan_rename_footprint(
                ROOT_DIR, category, old, new,
                rename_model=args.update_model_file, **common,
            )
        else:
            plan = rf.plan_rename_model(ROOT_DIR, category, old, new, **common)

    return _run_plan(plan, args, prov=prov)


def cmd_move(args: argparse.Namespace) -> int:
    prov = pv.load(ROOT_DIR)
    category, name = _split_lib_id(args.name, args.category, args.kind)
    plan = rf.plan_move(
        ROOT_DIR, args.kind, category, name, args.to,
        conflict=ops.ConflictPolicy(args.conflict),
    )
    return _run_plan(plan, args, prov=prov)


def cmd_package(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve() if args.out else None
    plan, result = packager_mod.plan_package(ROOT_DIR, Path(args.project), out)
    _out(result.summary())
    _out()
    code = _run_plan(plan, args, regenerate=False)
    if code == EXIT_OK and not args.dry_run and result.tables_updated:
        _out(f"updated project table(s): {', '.join(result.tables_updated)}")
    return code


def cmd_migrate_manifest(args: argparse.Namespace) -> int:
    prov, report = pv.migrate_manifest(ROOT_DIR)
    _out(report.summary())
    if not prov.items and not report.migrated:
        _out("\nNothing to write.")
        return EXIT_OK
    if args.dry_run:
        _out("\n--- provenance.json would contain ---")
        _out(prov.to_json_text())
        return EXIT_OK
    if not _confirm(f"\nWrite {pv.FILENAME}?", args.yes):
        _out("Aborted.")
        return EXIT_ABORTED
    prov.save()
    _out(f"wrote {prov.path.relative_to(ROOT_DIR)}")
    _out(f"manifest.json has been left in place; delete it once you are happy.")
    return EXIT_OK


def cmd_prune_provenance(args: argparse.Namespace) -> int:
    """
    Repair provenance entries whose item is no longer where they say it is.

    Nothing removes these automatically -- save() writes the items verbatim --
    so a library that has been reorganised accumulates one entry per abandoned
    name. Most are recoverable: when the kind and name still match exactly one
    item on disk, only the category was wrong, and the entry still holds the
    original vendor filename and import date. Those are re-homed. Only what
    cannot be placed unambiguously is dropped, and its details are printed
    first so the information is not lost silently.
    """
    try:
        prov = pv.load(ROOT_DIR)
    except pv.ProvenanceError as exc:
        _err(f"error: {exc}")
        return EXIT_ERROR

    if not prov.existed:
        _out(f"No {pv.FILENAME} to prune.")
        return EXIT_OK

    lib = lb.scan(ROOT_DIR)
    stale = pv.stale_keys(prov, lib)
    if not stale:
        _out(f"Nothing to do: all {len(prov.items)} entr"
             f"{'y' if len(prov.items) == 1 else 'ies'} match something on disk.")
        return EXIT_OK

    moves = {} if args.drop_only else pv.recoverable(prov, lib)
    drops = [k for k in stale if k not in moves]

    _out(f"{len(stale)} stale entr{'y' if len(stale) == 1 else 'ies'} "
         f"of {len(prov.items)}.\n")

    if moves:
        _out(f"Re-home {len(moves)} (same item, old category name):")
        for old_key in sorted(moves):
            _out(f"  {old_key}\n    -> {moves[old_key]}")
        _out("")

    if drops:
        _out(f"Drop {len(drops)} (cannot be placed unambiguously):")
        for key in drops:
            item = prov.items[key]
            bits = [b for b in (item.original_name, item.source, item.imported) if b]
            _out(f"  {key}" + (f"\n    was: {', '.join(bits)}" if bits else ""))
        _out("")

    if args.dry_run:
        _out("(dry run: nothing was changed)")
        return EXIT_OK
    if not _confirm("Apply?", args.yes):
        _out("Aborted.")
        return EXIT_ABORTED

    recovered = pv.recover(prov, lib) if not args.drop_only else {}
    removed = pv.prune(prov, lib)
    prov.save()
    _out(f"re-homed {len(recovered)}, removed {len(removed)}; "
         f"{len(prov.items)} entr{'y' if len(prov.items) == 1 else 'ies'} remain")
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    """Show what the library actually contains, straight from the disk."""
    lib = lb.scan(ROOT_DIR)
    if not lib.categories:
        _out("The library is empty.")
        return EXIT_OK

    refs = lib.resolve()
    for name in lib.category_names():
        cat = lib.categories[name]
        flag = "  (empty)" if cat.is_empty else ""
        _out(f"{name}{flag}")
        _out(f"  {cat.symbol_count} symbol(s), {cat.footprint_count} footprint(s), "
             f"{cat.model_count} model(s)")
        if args.verbose:
            for sym in lib.symbols_in(name):
                target = sym.footprint_ref or "(no footprint)"
                broken = " [BROKEN]" if any(
                    d.source == sym.lib_id for d in refs.dangling
                ) else ""
                _out(f"    symbol     {sym.name} -> {target}{broken}")
            for fp in lib.footprints_in(name):
                models = ", ".join(Path(p).name for p in fp.model_paths) or "(no model)"
                _out(f"    footprint  {fp.name} -> {models}")
            for model in lib.models_in(name):
                _out(f"    model      {model.filename}")
    _out(f"\nTotal: {len(lib.symbols)} symbol(s), {len(lib.footprints)} footprint(s), "
         f"{len(lib.models)} model(s) in {len(lib.categories)} categor(y/ies)")
    if tg.is_stale(ROOT_DIR, lib):
        _out("warning: the master tables are out of date; run 'generate'")
    return EXIT_OK


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from src.gui.app import launch_gui
    except ImportError as exc:
        if "tkinter" in str(exc).lower() or "_tkinter" in str(exc).lower():
            _err("The GUI needs Tk, which is not installed for this Python.")
            _err("Install it with one of:")
            for label, command in TK_HINT.items():
                _err(f"  {label:<8} {command}")
            _err("\nAll functionality is available from the CLI; run --help.")
        else:
            _err(f"Could not import the GUI: {exc}")
        if args.debug:
            traceback.print_exc()
        return EXIT_ERROR

    _out("Launching KiCad Custom Library Manager...")
    launch_gui(ROOT_DIR)
    return EXIT_OK


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lib_manager.py",
        description="Manage the KICAD_CUSTOM_LIB symbol, footprint and 3D model library.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  lib_manager.py list -v
  lib_manager.py check
  lib_manager.py ingest ~/Downloads/tpa3255.zip -c TI-TPAxxx_AUDIO-AMP --dry-run
  lib_manager.py ingest ~/Downloads/part.zip -c Conn_XT --conflict rename --yes
  lib_manager.py sync-staging --archive
  lib_manager.py rename footprint TI-TPAxxx_AUDIO-AMP:SOP63P810X120-44N SOP65P810X120-44N
  lib_manager.py rename category 3255 TI-TPAxxx_AUDIO-AMP
  lib_manager.py move symbol Conn_XT:XT60 --to Connector_XT
  lib_manager.py package ~/projects/amp --out ~/projects/amp/project_libs
  lib_manager.py migrate-manifest
  lib_manager.py prune-provenance --dry-run

Every mutating command prints its plan first. Add --dry-run to stop there, or
--yes to skip the confirmation.
""",
    )
    parser.add_argument("--debug", action="store_true",
                        help="print full tracebacks instead of a one-line error")

    def add_mutating(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--dry-run", "-n", action="store_true",
                         help="show the plan and stop")
        sub.add_argument("--yes", "-y", action="store_true",
                         help="do not ask for confirmation")
        sub.add_argument("--conflict", choices=[p.value for p in ops.ConflictPolicy],
                         default=ops.ConflictPolicy.SKIP.value,
                         help="what to do when the target already exists "
                              "(default: skip with a warning)")

    subparsers = parser.add_subparsers(dest="command")

    p_list = subparsers.add_parser("list", help="show what the library contains")
    p_list.add_argument("--verbose", "-v", action="store_true",
                        help="list every item, not just category totals")
    p_list.set_defaults(func=cmd_list)

    p_check = subparsers.add_parser(
        "check", help="audit the library (exits non-zero on errors)")
    p_check.add_argument("--json", action="store_true", help="machine-readable output")
    p_check.add_argument("--no-kicad-cli", action="store_true",
                         help="skip the KiCad parse checks even if kicad-cli is present")
    p_check.set_defaults(func=cmd_check)

    p_generate = subparsers.add_parser(
        "generate", help="regenerate sym-lib-table and fp-lib-table")
    add_mutating(p_generate)
    p_generate.set_defaults(func=cmd_generate)

    p_ingest = subparsers.add_parser(
        "ingest", help="import a ZIP, folder or single file into a category")
    p_ingest.add_argument("source", help="ZIP, folder, or a single .kicad_sym/.kicad_mod/.step")
    p_ingest.add_argument("--category", "-c", required=True, help="target category")
    p_ingest.add_argument("--select", nargs="+", metavar="NAME", default=None,
                          help="import only these items (default: all)")
    add_mutating(p_ingest)
    p_ingest.set_defaults(func=cmd_ingest)

    p_sync = subparsers.add_parser(
        "sync-staging", help="import everything under staging-temp/intake/<Category>/")
    p_sync.add_argument("--archive", action="store_true",
                        help="move imported items to staging-temp/imported_archive/")
    add_mutating(p_sync)
    p_sync.set_defaults(func=cmd_sync_staging)

    p_rename = subparsers.add_parser("rename", help="rename an item or a whole category")
    p_rename.add_argument("kind", choices=[rf.KIND_SYMBOL, rf.KIND_FOOTPRINT,
                                           rf.KIND_MODEL, rf.KIND_CATEGORY])
    p_rename.add_argument("old", help="current name, or 'Category:Name'")
    p_rename.add_argument("new", help="new name")
    p_rename.add_argument("--category", "-c", default=None,
                          help="category, when 'old' is a bare name")
    p_rename.add_argument("--update-model-file", action="store_true",
                          help="when renaming a footprint, rename its 3D model to match")
    add_mutating(p_rename)
    p_rename.set_defaults(func=cmd_rename)

    p_move = subparsers.add_parser("move", help="move an item into another category")
    p_move.add_argument("kind", choices=[rf.KIND_SYMBOL, rf.KIND_FOOTPRINT, rf.KIND_MODEL])
    p_move.add_argument("name", help="item name, or 'Category:Name'")
    p_move.add_argument("--to", "-t", required=True, help="destination category")
    p_move.add_argument("--category", "-c", default=None,
                        help="source category, when 'name' is a bare name")
    add_mutating(p_move)
    p_move.set_defaults(func=cmd_move)

    p_package = subparsers.add_parser(
        "package", help="export the custom parts a project uses into a self-contained bundle")
    p_package.add_argument("project", help="KiCad project directory")
    p_package.add_argument("--out", "-o", default=None,
                           help="bundle directory, inside the project "
                                "(default: <project>/project_libs)")
    add_mutating(p_package)
    p_package.set_defaults(func=cmd_package)

    p_migrate = subparsers.add_parser(
        "migrate-manifest", help="convert the old manifest.json into provenance.json")
    add_mutating(p_migrate)
    p_migrate.set_defaults(func=cmd_migrate_manifest)

    p_prune = subparsers.add_parser(
        "prune-provenance",
        help="drop provenance entries whose item is no longer on disk")
    p_prune.add_argument(
        "--drop-only", action="store_true",
        help="discard stale entries instead of re-homing the recoverable ones")
    add_mutating(p_prune)
    p_prune.set_defaults(func=cmd_prune_provenance)

    p_gui = subparsers.add_parser("gui", help="launch the desktop interface")
    p_gui.set_defaults(func=cmd_gui)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        args.func = cmd_gui

    try:
        return args.func(args)
    except SystemExit:
        raise
    except (UsageError, naming.NameError_, rf.RefactorError,
            packager_mod.PackageError, pv.ProvenanceError, ops.ApplyError,
            FileNotFoundError) as exc:
        _err(f"error: {exc}")
        if args.debug:
            traceback.print_exc()
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001
        _err(f"unexpected error: {type(exc).__name__}: {exc}")
        if args.debug:
            traceback.print_exc()
        else:
            _err("re-run with --debug for a full traceback")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
