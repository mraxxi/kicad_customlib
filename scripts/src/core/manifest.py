"""
Manifest database manager and component lifecycle coordinator for KICAD_CUSTOM_LIB.
Tracks original import sources, dates, file locations, and rename/reorganization history.
"""

import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from .s_expr import (
    patch_footprint_3d_model,
    patch_symbol_footprint,
    extract_footprint_model_info,
    get_symbol_footprint_id,
)
from .table_gen import generate_tables


class ManifestManager:
    def __init__(self, root_dir: Path):
        self.root_dir = root_dir.resolve()
        self.manifest_path = self.root_dir / "manifest.json"
        self.data: Dict[str, Any] = self._load_manifest()

    def _load_manifest(self) -> Dict[str, Any]:
        if self.manifest_path.exists():
            try:
                return json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "version": 1,
            "library_name": "KICAD_CUSTOM_LIB",
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "parts": {},
            "history": [],
        }

    def save(self) -> None:
        self.data["last_updated"] = datetime.now(timezone.utc).isoformat()
        self.manifest_path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8"
        )

    def get_part(self, part_name: str) -> Optional[Dict[str, Any]]:
        return self.data["parts"].get(part_name)

    def list_parts(self, category: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        if not category:
            return self.data["parts"]
        return {
            k: v for k, v in self.data["parts"].items()
            if v.get("category") == category
        }

    def list_categories(self) -> List[str]:
        categories = set()
        for v in self.data["parts"].values():
            if "category" in v:
                categories.add(v["category"])
        
        # Also check existing disk directories
        symbols_dir = self.root_dir / "symbols"
        if symbols_dir.exists():
            for p in symbols_dir.iterdir():
                if p.is_dir() and p.name.endswith(".kicad_symdir"):
                    categories.add(p.name[:-len(".kicad_symdir")])
        return sorted(list(categories))

    def ingest_part(
        self,
        source_path: Path,
        category: str,
        part_name: Optional[str] = None,
        source_meta: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Ingests a component from a folder or ZIP file into the library.
        Mirrors the category across symbols, footprints, and 3dmodels.
        """
        source_path = Path(source_path).resolve()
        temp_dir: Optional[Path] = None

        if source_path.is_file() and zipfile.is_zipfile(source_path):
            import tempfile
            temp_dir = Path(tempfile.mkdtemp(prefix="kicad_ingest_"))
            with zipfile.ZipFile(source_path, "r") as zf:
                zf.extractall(temp_dir)
            search_root = temp_dir
        else:
            search_root = source_path

        # Locate symbol, footprint, and 3D files
        sym_files = list(search_root.rglob("*.kicad_sym"))
        mod_files = list(search_root.rglob("*.kicad_mod"))
        step_files = (
            list(search_root.rglob("*.step"))
            + list(search_root.rglob("*.stp"))
            + list(search_root.rglob("*.wrl"))
        )

        detected_part = part_name
        if not detected_part:
            if sym_files:
                detected_part = sym_files[0].stem
            elif mod_files:
                detected_part = mod_files[0].stem
            elif step_files:
                detected_part = step_files[0].stem
            else:
                detected_part = source_path.stem

        # Ensure destination mirrored directories exist
        sym_dir = self.root_dir / "symbols" / f"{category}.kicad_symdir"
        fp_dir = self.root_dir / "footprints" / f"{category}.pretty"
        model_dir = self.root_dir / "3dmodels" / f"{category}.3dshapes"

        sym_dir.mkdir(parents=True, exist_ok=True)
        fp_dir.mkdir(parents=True, exist_ok=True)
        model_dir.mkdir(parents=True, exist_ok=True)

        dest_files = {}

        # Copy symbol
        if sym_files:
            src_sym = sym_files[0]
            dest_sym = sym_dir / f"{detected_part}.kicad_sym"
            shutil.copy2(src_sym, dest_sym)
            dest_files["symbol"] = str(dest_sym.relative_to(self.root_dir))

        # Copy 3D model
        dest_model_rel: Optional[str] = None
        if step_files:
            src_step = step_files[0]
            dest_step = model_dir / f"{detected_part}.step"
            shutil.copy2(src_step, dest_step)
            dest_files["model_3d"] = str(dest_step.relative_to(self.root_dir))
            dest_model_rel = f"${{KICAD_CUSTOM_LIB}}/3dmodels/{category}.3dshapes/{detected_part}.step"

        # Copy footprint & patch references
        footprint_id = None
        if mod_files:
            src_mod = mod_files[0]
            fp_filename = src_mod.name
            dest_mod = fp_dir / fp_filename
            shutil.copy2(src_mod, dest_mod)
            dest_files["footprint"] = str(dest_mod.relative_to(self.root_dir))

            # Patch 3D model path if model exists
            if dest_model_rel:
                patch_footprint_3d_model(dest_mod, dest_model_rel)

            fp_basename = dest_mod.stem
            footprint_id = f"{category}:{fp_basename}"

        # Patch symbol footprint property
        if "symbol" in dest_files and footprint_id:
            patch_symbol_footprint(self.root_dir / dest_files["symbol"], footprint_id)

        # Cleanup temp unzipped if created
        if temp_dir and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

        # Record in manifest
        now_str = datetime.now(timezone.utc).isoformat()
        record = {
            "display_name": detected_part,
            "category": category,
            "original_import_name": source_path.name,
            "import_date": now_str,
            "source_meta": source_meta or "",
            "files": dest_files,
            "footprint_identifier": footprint_id or "",
        }
        self.data["parts"][detected_part] = record
        self.data["history"].append({
            "timestamp": now_str,
            "action": "ingest",
            "part": detected_part,
            "category": category,
            "source": source_path.name,
        })
        self.save()

        # Regenerate tables
        generate_tables(self.root_dir)
        return record

    def move_part(self, part_name: str, new_category: str) -> bool:
        """
        Moves a part and all its associated files to a new category.
        Updates internal S-expression references, manifest, and tables.
        """
        record = self.data["parts"].get(part_name)
        if not record:
            return False

        old_category = record.get("category", "")
        if old_category == new_category:
            return True

        # Target directories
        new_sym_dir = self.root_dir / "symbols" / f"{new_category}.kicad_symdir"
        new_fp_dir = self.root_dir / "footprints" / f"{new_category}.pretty"
        new_model_dir = self.root_dir / "3dmodels" / f"{new_category}.3dshapes"

        new_sym_dir.mkdir(parents=True, exist_ok=True)
        new_fp_dir.mkdir(parents=True, exist_ok=True)
        new_model_dir.mkdir(parents=True, exist_ok=True)

        files = record.get("files", {})
        new_files = {}

        # Move symbol
        if "symbol" in files:
            old_sym_p = self.root_dir / files["symbol"]
            if old_sym_p.exists():
                new_sym_p = new_sym_dir / old_sym_p.name
                shutil.move(old_sym_p, new_sym_p)
                new_files["symbol"] = str(new_sym_p.relative_to(self.root_dir))

        # Move 3D model
        new_3d_env_path = None
        if "model_3d" in files:
            old_3d_p = self.root_dir / files["model_3d"]
            if old_3d_p.exists():
                new_3d_p = new_model_dir / old_3d_p.name
                shutil.move(old_3d_p, new_3d_p)
                new_files["model_3d"] = str(new_3d_p.relative_to(self.root_dir))
                new_3d_env_path = f"${{KICAD_CUSTOM_LIB}}/3dmodels/{new_category}.3dshapes/{new_3d_p.name}"

        # Move footprint & patch 3D model path
        new_fp_id = None
        if "footprint" in files:
            old_fp_p = self.root_dir / files["footprint"]
            if old_fp_p.exists():
                new_fp_p = new_fp_dir / old_fp_p.name
                shutil.move(old_fp_p, new_fp_p)
                new_files["footprint"] = str(new_fp_p.relative_to(self.root_dir))
                if new_3d_env_path:
                    patch_footprint_3d_model(new_fp_p, new_3d_env_path)
                new_fp_id = f"{new_category}:{new_fp_p.stem}"

        # Patch symbol footprint property
        if "symbol" in new_files and new_fp_id:
            patch_symbol_footprint(self.root_dir / new_files["symbol"], new_fp_id)

        # Update record
        record["category"] = new_category
        record["files"] = new_files
        if new_fp_id:
            record["footprint_identifier"] = new_fp_id

        now_str = datetime.now(timezone.utc).isoformat()
        self.data["history"].append({
            "timestamp": now_str,
            "action": "move",
            "part": part_name,
            "from_category": old_category,
            "to_category": new_category,
        })
        self.save()

        # Clean up empty old dirs if any
        for d in [
            self.root_dir / "symbols" / f"{old_category}.kicad_symdir",
            self.root_dir / "footprints" / f"{old_category}.pretty",
            self.root_dir / "3dmodels" / f"{old_category}.3dshapes",
        ]:
            if d.exists() and not any(d.iterdir()):
                shutil.rmtree(d, ignore_errors=True)

        generate_tables(self.root_dir)
        return True

    def audit_health(self) -> Dict[str, Any]:
        """Audits the library for missing files, unlinked footprints, or orphaned items."""
        issues = []
        valid_parts = 0

        for part_name, record in self.data["parts"].items():
            files = record.get("files", {})
            has_error = False

            # Check files on disk
            for file_type, rel_path in files.items():
                abs_p = self.root_dir / rel_path
                if not abs_p.exists():
                    issues.append({
                        "part": part_name,
                        "type": "missing_file",
                        "detail": f"File not found: {rel_path} ({file_type})"
                    })
                    has_error = True

            # Check footprint 3D model link
            if "footprint" in files:
                fp_p = self.root_dir / files["footprint"]
                if fp_p.exists():
                    info = extract_footprint_model_info(fp_p)
                    if "model_3d" in files and not info:
                        issues.append({
                            "part": part_name,
                            "type": "unlinked_3d",
                            "detail": f"Footprint {fp_p.name} has no 3D model tag"
                        })

            if not has_error:
                valid_parts += 1

        return {
            "total_parts": len(self.data["parts"]),
            "valid_parts": valid_parts,
            "issues": issues,
            "healthy": len(issues) == 0,
        }
