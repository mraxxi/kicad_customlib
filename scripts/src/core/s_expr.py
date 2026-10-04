"""
S-Expression parsing and safe mutation utilities for KiCad files.
Preserves UUIDs, formatting, and prevents file corruption.
"""

import re
from pathlib import Path
from typing import Optional, Dict, Any, Tuple


def find_matching_paren(text: str, start_index: int) -> int:
    """Find index of the matching closing parenthesis given opening paren index."""
    depth = 0
    in_quote = False
    escape = False

    for i in range(start_index, len(text)):
        char = text[i]
        if escape:
            escape = False
            continue
        if char == '\\':
            escape = True
            continue
        if char == '"':
            in_quote = not in_quote
            continue
        if not in_quote:
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    return i
    return -1


def extract_footprint_model_info(kicad_mod_path: Path) -> Optional[Dict[str, Any]]:
    """Extract model path, offset, scale, and rotate from a .kicad_mod file."""
    try:
        content = kicad_mod_path.read_text(encoding="utf-8")
    except Exception:
        return None

    model_pos = content.find("(model ")
    if model_pos == -1:
        return None

    end_pos = find_matching_paren(content, model_pos)
    if end_pos == -1:
        return None

    model_block = content[model_pos:end_pos + 1]

    # Extract model path string
    path_match = re.search(r'\(model\s+"([^"]+)"', model_block)
    model_path = path_match.group(1) if path_match else ""

    return {
        "raw_block": model_block,
        "path": model_path,
        "start": model_pos,
        "end": end_pos,
    }


def patch_footprint_3d_model(
    kicad_mod_path: Path,
    new_model_path: str,
    default_offset: Tuple[float, float, float] = (0.0, 0.0, 0.0),
    default_scale: Tuple[float, float, float] = (1.0, 1.0, 1.0),
    default_rotate: Tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> bool:
    """
    Safely updates or adds the 3D model path in a .kicad_mod file.
    Preserves existing offset/scale/rotate if a model block already exists.
    """
    content = kicad_mod_path.read_text(encoding="utf-8")
    info = extract_footprint_model_info(kicad_mod_path)

    if info:
        # Existing model block: replace path string inside the block
        old_block = info["raw_block"]
        new_block = re.sub(
            r'\(model\s+"[^"]+"',
            f'(model "{new_model_path}"',
            old_block,
            count=1
        )
        new_content = content[:info["start"]] + new_block + content[info["end"] + 1:]
    else:
        # No model block: insert before the final closing parenthesis of the footprint
        last_paren = content.rfind(')')
        if last_paren == -1:
            return False

        model_block = (
            f'\n\t(model "{new_model_path}"\n'
            f'\t\t(offset (xyz {default_offset[0]} {default_offset[1]} {default_offset[2]}))\n'
            f'\t\t(scale (xyz {default_scale[0]} {default_scale[1]} {default_scale[2]}))\n'
            f'\t\t(rotate (xyz {default_rotate[0]} {default_rotate[1]} {default_rotate[2]}))\n'
            f'\t)\n'
        )
        new_content = content[:last_paren] + model_block + content[last_paren:]

    kicad_mod_path.write_text(new_content, encoding="utf-8")
    return True


def patch_symbol_footprint(kicad_sym_path: Path, new_footprint_id: str) -> bool:
    """
    Safely updates the (property "Footprint" ...) field in a .kicad_sym file.
    Preserves other attributes, position, and font styling.
    """
    content = kicad_sym_path.read_text(encoding="utf-8")

    # Match (property "Footprint" "OLD_VAL" ...)
    pattern = r'(\(property\s+"Footprint"\s+)"[^"]*"'
    if re.search(pattern, content):
        new_content = re.sub(pattern, rf'\g<1>"{new_footprint_id}"', content, count=1)
        kicad_sym_path.write_text(new_content, encoding="utf-8")
        return True
    return False


def get_symbol_footprint_id(kicad_sym_path: Path) -> Optional[str]:
    """Extract current Footprint property from a .kicad_sym file."""
    try:
        content = kicad_sym_path.read_text(encoding="utf-8")
        match = re.search(r'\(property\s+"Footprint"\s+"([^"]*)"', content)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None
