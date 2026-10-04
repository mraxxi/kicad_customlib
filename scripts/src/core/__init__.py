"""Core library management modules."""
from .s_expr import patch_footprint_3d_model, patch_symbol_footprint, extract_footprint_model_info
from .table_gen import generate_tables, scan_libraries
from .manifest import ManifestManager
from .packager import ProjectPackager

__all__ = [
    "patch_footprint_3d_model",
    "patch_symbol_footprint",
    "extract_footprint_model_info",
    "generate_tables",
    "scan_libraries",
    "ManifestManager",
    "ProjectPackager",
]
