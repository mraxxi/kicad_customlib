"""
Generates master sym-lib-table and fp-lib-table for KICAD_CUSTOM_LIB.
Uses Version 7 format and standardized ${KICAD_CUSTOM_LIB} environment variable.
"""

from pathlib import Path
from typing import Dict, List, Tuple


def scan_libraries(root_dir: Path) -> Dict[str, List[Tuple[str, Path, str]]]:
    """
    Scans symbols/ and footprints/ directories for valid libraries.
    Returns dictionary with 'symbols' and 'footprints' lists.
    """
    symbols_dir = root_dir / "symbols"
    footprints_dir = root_dir / "footprints"

    sym_libs: List[Tuple[str, Path, str]] = []
    fp_libs: List[Tuple[str, Path, str]] = []

    # Scan symbols: .kicad_symdir directories or standalone .kicad_sym files
    if symbols_dir.exists():
        for item in sorted(symbols_dir.iterdir()):
            if item.is_dir() and item.name.endswith(".kicad_symdir"):
                name = item.name[:-len(".kicad_symdir")]
                rel_path = f"${{KICAD_CUSTOM_LIB}}/symbols/{item.name}"
                sym_libs.append((name, item, rel_path))
            elif item.is_file() and item.name.endswith(".kicad_sym"):
                name = item.stem
                rel_path = f"${{KICAD_CUSTOM_LIB}}/symbols/{item.name}"
                sym_libs.append((name, item, rel_path))

    # Scan footprints: .pretty directories
    if footprints_dir.exists():
        for item in sorted(footprints_dir.iterdir()):
            if item.is_dir() and item.name.endswith(".pretty"):
                name = item.name[:-len(".pretty")]
                rel_path = f"${{KICAD_CUSTOM_LIB}}/footprints/{item.name}"
                fp_libs.append((name, item, rel_path))

    return {
        "symbols": sym_libs,
        "footprints": fp_libs,
    }


def generate_sym_lib_table(root_dir: Path) -> Path:
    """Generates the master sym-lib-table at the repository root."""
    libs = scan_libraries(root_dir)["symbols"]
    table_path = root_dir / "sym-lib-table"

    lines = [
        "(sym_lib_table",
        "\t(version 7)",
    ]

    for name, _, uri in libs:
        lines.append(
            f'\t(lib (name "{name}") (type "KiCad") (uri "{uri}") (options "") (descr "Custom symbol library {name}"))'
        )

    lines.append(")\n")
    table_path.write_text("\n".join(lines), encoding="utf-8")
    return table_path


def generate_fp_lib_table(root_dir: Path) -> Path:
    """Generates the master fp-lib-table at the repository root."""
    libs = scan_libraries(root_dir)["footprints"]
    table_path = root_dir / "fp-lib-table"

    lines = [
        "(fp_lib_table",
        "\t(version 7)",
    ]

    for name, _, uri in libs:
        lines.append(
            f'\t(lib (name "{name}") (type "KiCad") (uri "{uri}") (options "") (descr "Custom footprint library {name}"))'
        )

    lines.append(")\n")
    table_path.write_text("\n".join(lines), encoding="utf-8")
    return table_path


def generate_tables(root_dir: Path) -> Tuple[Path, Path]:
    """Generates both sym-lib-table and fp-lib-table."""
    sym_table = generate_sym_lib_table(root_dir)
    fp_table = generate_fp_lib_table(root_dir)
    return sym_table, fp_table
