"""
Lean project packager / exporter for KICAD_CUSTOM_LIB.
Extracts only the custom components used in a target project into a self-contained
bundle using ${KIPRJMOD}, making it lightweight and ready for public GitHub release.
"""

import re
import shutil
from pathlib import Path
from typing import Dict, Set, List, Tuple, Any, Optional

from .s_expr import patch_footprint_3d_model


class ProjectPackager:
    def __init__(self, lib_root: Path):
        self.lib_root = lib_root.resolve()

    def find_used_libraries_in_project(self, project_dir: Path) -> Tuple[Set[str], Set[str]]:
        """
        Scans all .kicad_sch and .kicad_pcb files in project_dir.
        Returns (used_symbol_identifiers, used_footprint_identifiers).
        Example identifiers: 'TI-TPAxxx_AUDIO-AMP:TPA3255DDV', 'TI-TPAxxx_AUDIO-AMP:SOP63P810X120-44N'
        """
        project_dir = Path(project_dir).resolve()
        used_syms: Set[str] = set()
        used_fps: Set[str] = set()

        # Scan schematics
        for sch_file in project_dir.rglob("*.kicad_sch"):
            try:
                content = sch_file.read_text(encoding="utf-8")
                # Find (lib_id "LibName:SymbolName")
                for match in re.finditer(r'\(lib_id\s+"([^"]+:[^"]+)"\)', content):
                    used_syms.add(match.group(1))
                # Find footprint properties (property "Footprint" "LibName:FootprintName")
                for match in re.finditer(r'\(property\s+"Footprint"\s+"([^"]+:[^"]+)"', content):
                    used_fps.add(match.group(1))
            except Exception:
                pass

        # Scan PCB
        for pcb_file in project_dir.rglob("*.kicad_pcb"):
            try:
                content = pcb_file.read_text(encoding="utf-8")
                # Find (footprint "LibName:FootprintName" ...)
                for match in re.finditer(r'\(footprint\s+"([^"]+:[^"]+)"', content):
                    used_fps.add(match.group(1))
            except Exception:
                pass

        return used_syms, used_fps

    def package_for_project(
        self,
        project_dir: Path,
        output_dir: Optional[Path] = None
    ) -> Dict[str, Any]:
        """
        Packages all required custom library components into output_dir
        (defaults to <project_dir>/project_libs).
        Generates local sym-lib-table and fp-lib-table pointing to ${KIPRJMOD}/project_libs.
        """
        project_dir = Path(project_dir).resolve()
        out = (output_dir or (project_dir / "project_libs")).resolve()
        out.mkdir(parents=True, exist_ok=True)

        syms_dir = out / "symbols"
        fps_dir = out / "footprints"
        models_dir = out / "3dmodels"

        syms_dir.mkdir(parents=True, exist_ok=True)
        fps_dir.mkdir(parents=True, exist_ok=True)
        models_dir.mkdir(parents=True, exist_ok=True)

        used_syms, used_fps = self.find_used_libraries_in_project(project_dir)

        packaged_categories: Set[str] = set()
        packaged_symbols: List[str] = []
        packaged_footprints: List[str] = []

        # Process symbols
        for sym_id in used_syms:
            if ":" not in sym_id:
                continue
            cat, sym_name = sym_id.split(":", 1)
            # Check if this category exists in custom lib
            src_cat_symdir = self.lib_root / "symbols" / f"{cat}.kicad_symdir"
            src_sym_file = src_cat_symdir / f"{sym_name}.kicad_sym"

            if src_sym_file.exists():
                dst_cat_symdir = syms_dir / f"{cat}.kicad_symdir"
                dst_cat_symdir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_sym_file, dst_cat_symdir / f"{sym_name}.kicad_sym")
                packaged_categories.add(cat)
                packaged_symbols.append(sym_id)

        # Process footprints
        for fp_id in used_fps:
            if ":" not in fp_id:
                continue
            cat, fp_name = fp_id.split(":", 1)
            src_pretty = self.lib_root / "footprints" / f"{cat}.pretty"
            src_mod = src_pretty / f"{fp_name}.kicad_mod"

            if src_mod.exists():
                dst_pretty = fps_dir / f"{cat}.pretty"
                dst_pretty.mkdir(parents=True, exist_ok=True)
                dst_mod = dst_pretty / f"{fp_name}.kicad_mod"
                shutil.copy2(src_mod, dst_mod)
                packaged_categories.add(cat)
                packaged_footprints.append(fp_id)

                # Check if 3D model exists for this category/part
                src_3d_dir = self.lib_root / "3dmodels" / f"{cat}.3dshapes"
                if src_3d_dir.exists():
                    dst_3d_dir = models_dir / f"{cat}.3dshapes"
                    dst_3d_dir.mkdir(parents=True, exist_ok=True)
                    # Copy matching step files
                    for step_file in src_3d_dir.glob(f"{fp_name}*.step"):
                        shutil.copy2(step_file, dst_3d_dir / step_file.name)
                        # Patch local 3D path for standalone bundle
                        patch_footprint_3d_model(
                            dst_mod,
                            f"${{KIPRJMOD}}/project_libs/3dmodels/{cat}.3dshapes/{step_file.name}"
                        )

        # Generate standalone project-level sym-lib-table & fp-lib-table
        sym_table_lines = ["(sym_lib_table", "\t(version 7)"]
        fp_table_lines = ["(fp_lib_table", "\t(version 7)"]

        for cat in sorted(packaged_categories):
            sym_table_lines.append(
                f'\t(lib (name "{cat}") (type "KiCad") (uri "${{KIPRJMOD}}/project_libs/symbols/{cat}.kicad_symdir") (options "") (descr "Packaged library {cat}"))'
            )
            fp_table_lines.append(
                f'\t(lib (name "{cat}") (type "KiCad") (uri "${{KIPRJMOD}}/project_libs/footprints/{cat}.pretty") (options "") (descr "Packaged library {cat}"))'
            )

        sym_table_lines.append(")\n")
        fp_table_lines.append(")\n")

        (out / "sym-lib-table").write_text("\n".join(sym_table_lines), encoding="utf-8")
        (out / "fp-lib-table").write_text("\n".join(fp_table_lines), encoding="utf-8")

        return {
            "output_directory": str(out),
            "packaged_categories": list(packaged_categories),
            "packaged_symbols": packaged_symbols,
            "packaged_footprints": packaged_footprints,
        }
