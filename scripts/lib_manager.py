#!/usr/bin/env python3
"""
KiCad Custom Library Manager (KICAD_CUSTOM_LIB)
Unified CLI and GUI utility for managing symbols, footprints, 3D models,
staging intake, manifest database, and standalone project packaging.
"""

import sys
import argparse
from pathlib import Path

# Add scripts directory to module path
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from src.core.table_gen import generate_tables
from src.core.manifest import ManifestManager
from src.core.packager import ProjectPackager


def main():
    parser = argparse.ArgumentParser(
        description="KiCad Custom Library Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/lib_manager.py                           # Launch Desktop GUI
  python scripts/lib_manager.py generate                  # Regenerate sym-lib-table & fp-lib-table
  python scripts/lib_manager.py check                     # Audit library health
  python scripts/lib_manager.py ingest /path/to/part.zip --category TI-TPAxxx_AUDIO-AMP
  python scripts/lib_manager.py move --part TPA3255 --to TI-TPAxxx_AUDIO-AMP
  python scripts/lib_manager.py package /path/to/project  # Export lean standalone project bundle
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    # Command: generate
    subparsers.add_parser("generate", help="Regenerate master sym-lib-table and fp-lib-table")

    # Command: check
    subparsers.add_parser("check", help="Audit library health for missing files or broken links")

    # Command: ingest
    ingest_parser = subparsers.add_parser("ingest", help="Ingest a component from folder or ZIP archive")
    ingest_parser.add_argument("source", type=str, help="Path to component ZIP or directory")
    ingest_parser.add_argument("--category", "-c", required=True, type=str, help="Target library category")
    ingest_parser.add_argument("--part", "-p", type=str, default=None, help="Optional custom part name")

    # Command: sync-staging
    subparsers.add_parser("sync-staging", help="Process and ingest all parts inside staging-temp/intake/")

    # Command: move
    move_parser = subparsers.add_parser("move", help="Move/Reorganize a component into a new category")
    move_parser.add_argument("--part", "-p", required=True, type=str, help="Name of part to move")
    move_parser.add_argument("--to", "-t", required=True, type=str, help="Target category name")

    # Command: package
    pkg_parser = subparsers.add_parser("package", help="Export lean standalone library bundle for a project")
    pkg_parser.add_argument("project", type=str, help="Path to KiCad project directory")
    pkg_parser.add_argument("--out", "-o", type=str, default=None, help="Output destination folder")

    # Command: gui
    subparsers.add_parser("gui", help="Launch desktop GUI interface")

    args = parser.parse_args()

    # Default to GUI if no command specified
    if args.command is None or args.command == "gui":
        try:
            from src.gui.app import launch_gui
            print("Launching KiCad Custom Library Manager GUI...")
            launch_gui(ROOT_DIR)
        except Exception as e:
            print(f"Failed to launch GUI: {e}")
            print("Run with --help for CLI commands.")
        return

    # CLI Command Dispatch
    manifest = ManifestManager(ROOT_DIR)

    if args.command == "generate":
        sym_p, fp_p = generate_tables(ROOT_DIR)
        print(f"✓ Generated {sym_p.relative_to(ROOT_DIR)}")
        print(f"✓ Generated {fp_p.relative_to(ROOT_DIR)}")

    elif args.command == "check":
        res = manifest.audit_health()
        print(f"--- Library Health Audit ---")
        print(f"Total parts: {res['total_parts']} | Valid: {res['valid_parts']}")
        if res["healthy"]:
            print("✓ Library is 100% healthy. No missing files or broken references.")
        else:
            print(f"✗ Found {len(res['issues'])} issues:")
            for issue in res["issues"]:
                print(f"  • [{issue['part']}] ({issue['type']}): {issue['detail']}")

    elif args.command == "ingest":
        print(f"Ingesting '{args.source}' into category '{args.category}'...")
        rec = manifest.ingest_part(Path(args.source), args.category, args.part)
        print(f"✓ Ingested '{rec['display_name']}' successfully.")
        print(f"  Footprint ID: {rec.get('footprint_identifier')}")

    elif args.command == "sync-staging":
        intake_dir = ROOT_DIR / "staging-temp" / "intake"
        if not intake_dir.exists() or not any(intake_dir.iterdir()):
            print(f"Staging directory {intake_dir} is empty.")
            return
        count = 0
        for cat_dir in intake_dir.iterdir():
            if cat_dir.is_dir():
                for part_dir in cat_dir.iterdir():
                    if part_dir.is_dir():
                        rec = manifest.ingest_part(part_dir, cat_dir.name)
                        print(f"✓ Ingested {rec['display_name']} -> {cat_dir.name}")
                        count += 1
        print(f"✓ Processed {count} parts from staging.")

    elif args.command == "move":
        ok = manifest.move_part(args.part, args.to)
        if ok:
            print(f"✓ Moved '{args.part}' to category '{args.to}' and updated references.")
        else:
            print(f"✗ Failed to move '{args.part}'. Check if part exists in manifest.")

    elif args.command == "package":
        packager = ProjectPackager(ROOT_DIR)
        out_p = Path(args.out) if args.out else None
        res = packager.package_for_project(Path(args.project), out_p)
        print(f"✓ Packaged project '{args.project}':")
        print(f"  Destination: {res['output_directory']}")
        print(f"  Symbols:     {len(res['packaged_symbols'])}")
        print(f"  Footprints:  {len(res['packaged_footprints'])}")
        print(f"  Categories:  {', '.join(res['packaged_categories'])}")


if __name__ == "__main__":
    main()
