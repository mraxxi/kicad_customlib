"""
Builders for synthetic KiCad 10 files and vendor ZIP bundles.

Everything here is derived from the real files shipped in /usr/share/kicad
(KiCad 10.0.6) and from the vendor bundle sitting in staging-temp/. See
AGENTS.md section "KiCad 10 on-disk facts" for the invariants these encode.

Kept separate from conftest.py so tests can also call the builders directly
with custom names instead of only through fixtures.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Iterable, Optional, Sequence

# Format versions as emitted by KiCad 10.0.6.
SYM_VERSION = "20251024"
MOD_VERSION = "20260206"


# --------------------------------------------------------------------------
# Symbols
# --------------------------------------------------------------------------

def symbol_block(
    name: str,
    footprint: str = "",
    *,
    extends: Optional[str] = None,
    units: Sequence[str] = ("1_1",),
    datasheet: str = "",
) -> str:
    """
    One top-level (symbol ...) block, indented one tab, as KiCad writes it.

    Unit sub-symbols are named "<name>_<unit>_<style>" and nested one level
    deeper -- that naming is what makes renaming a symbol a multi-site edit.
    A derived symbol carries (extends "<parent>") and has no unit blocks.
    """
    lines = [f'\t(symbol "{name}"']
    if extends is not None:
        lines.append(f'\t\t(extends "{extends}")')
    else:
        lines += [
            "\t\t(exclude_from_sim no)",
            "\t\t(in_bom yes)",
            "\t\t(on_board yes)",
        ]
    lines += [
        '\t\t(property "Reference" "U"',
        "\t\t\t(at 0 0 0)",
        "\t\t\t(effects",
        "\t\t\t\t(font",
        "\t\t\t\t\t(size 1.27 1.27)",
        "\t\t\t\t)",
        "\t\t\t)",
        "\t\t)",
        f'\t\t(property "Value" "{name}"',
        "\t\t\t(at 0 2.54 0)",
        "\t\t\t(effects",
        "\t\t\t\t(font",
        "\t\t\t\t\t(size 1.27 1.27)",
        "\t\t\t\t)",
        "\t\t\t)",
        "\t\t)",
        f'\t\t(property "Footprint" "{footprint}"',
        "\t\t\t(at 0 5.08 0)",
        "\t\t\t(hide yes)",
        "\t\t\t(effects",
        "\t\t\t\t(font",
        "\t\t\t\t\t(size 1.27 1.27)",
        "\t\t\t\t)",
        "\t\t\t)",
        "\t\t)",
        f'\t\t(property "Datasheet" "{datasheet}"',
        "\t\t\t(at 0 7.62 0)",
        "\t\t\t(hide yes)",
        "\t\t\t(effects",
        "\t\t\t\t(font",
        "\t\t\t\t\t(size 1.27 1.27)",
        "\t\t\t\t)",
        "\t\t\t)",
        "\t\t)",
    ]
    if extends is None:
        for unit in units:
            lines += [
                f'\t\t(symbol "{name}_{unit}"',
                "\t\t\t(rectangle",
                "\t\t\t\t(start -5.08 -5.08)",
                "\t\t\t\t(end 5.08 5.08)",
                "\t\t\t\t(stroke",
                "\t\t\t\t\t(width 0.254)",
                "\t\t\t\t\t(type default)",
                "\t\t\t\t)",
                "\t\t\t\t(fill",
                "\t\t\t\t\t(type background)",
                "\t\t\t\t)",
                "\t\t\t)",
                "\t\t\t(pin passive line",
                "\t\t\t\t(at -7.62 0 0)",
                "\t\t\t\t(length 2.54)",
                '\t\t\t\t(name "IN"',
                "\t\t\t\t\t(effects",
                "\t\t\t\t\t\t(font",
                "\t\t\t\t\t\t\t(size 1.27 1.27)",
                "\t\t\t\t\t\t)",
                "\t\t\t\t\t)",
                "\t\t\t\t)",
                '\t\t\t\t(number "1"',
                "\t\t\t\t\t(effects",
                "\t\t\t\t\t\t(font",
                "\t\t\t\t\t\t\t(size 1.27 1.27)",
                "\t\t\t\t\t\t)",
                "\t\t\t\t\t)",
                "\t\t\t\t)",
                "\t\t\t)",
                "\t\t)",
            ]
    lines.append("\t)")
    return "\n".join(lines)


def symbol_lib(blocks: Iterable[str]) -> str:
    """Wrap symbol blocks in a (kicad_symbol_lib ...) envelope, LF, trailing newline."""
    body = "\n".join(blocks)
    return (
        "(kicad_symbol_lib\n"
        f"\t(version {SYM_VERSION})\n"
        '\t(generator "kicad_symbol_editor")\n'
        '\t(generator_version "10.0")\n'
        f"{body}\n"
        ")\n"
    )


def write_symbol(path: Path, name: Optional[str] = None, footprint: str = "", **kw) -> Path:
    """Single-symbol file, the shape every official .kicad_sym has."""
    path.parent.mkdir(parents=True, exist_ok=True)
    name = name if name is not None else path.stem
    path.write_text(symbol_lib([symbol_block(name, footprint, **kw)]), encoding="utf-8", newline="\n")
    return path


def write_multi_symbol(path: Path, names: Sequence[str], footprint_prefix: str = "") -> Path:
    """
    Several top-level symbols in one file -- what a project cache library or a
    hand-built vendor bundle looks like. No official library does this, so
    ingest has to split it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = [
        symbol_block(n, f"{footprint_prefix}:{n}" if footprint_prefix else "")
        for n in names
    ]
    path.write_text(symbol_lib(blocks), encoding="utf-8", newline="\n")
    return path


# --------------------------------------------------------------------------
# Footprints
# --------------------------------------------------------------------------

def footprint_text(
    name: str,
    *,
    model: Optional[str] = None,
    models: Sequence[str] = (),
    offset=(0.0, 0.0, 0.0),
    scale=(1.0, 1.0, 1.0),
    rotate=(0.0, 0.0, 0.0),
) -> str:
    """A footprint with zero, one, or several (model ...) blocks."""
    paths = list(models) if models else ([model] if model else [])
    lines = [
        f'(footprint "{name}"',
        f"\t(version {MOD_VERSION})",
        '\t(generator "pcbnew")',
        '\t(generator_version "10.0")',
        "\t(layer \"F.Cu\")",
        '\t(property "Reference" "REF**"',
        "\t\t(at 0 -3 0)",
        '\t\t(layer "F.SilkS")',
        '\t\t(uuid "b0a1f2c3-d4e5-4f60-8a9b-0c1d2e3f4a5b")',
        "\t\t(effects",
        "\t\t\t(font",
        "\t\t\t\t(size 1 1)",
        "\t\t\t\t(thickness 0.15)",
        "\t\t\t)",
        "\t\t)",
        "\t)",
        '\t(pad "1" smd rect',
        "\t\t(at -1.5 0)",
        "\t\t(size 1.2 0.6)",
        '\t\t(layers "F.Cu" "F.Paste" "F.Mask")',
        '\t\t(uuid "11112222-3333-4444-5555-666677778888")',
        "\t)",
        '\t(pad "2" smd rect',
        "\t\t(at 1.5 0)",
        "\t\t(size 1.2 0.6)",
        '\t\t(layers "F.Cu" "F.Paste" "F.Mask")',
        '\t\t(uuid "99990000-aaaa-bbbb-cccc-ddddeeeeffff")',
        "\t)",
    ]
    for p in paths:
        # KiCad escapes backslashes inside quoted tokens; a raw Windows path
        # written verbatim would be an invalidly-escaped file.
        esc = p.replace("\\", "\\\\").replace('"', '\\"')
        lines += [
            f'\t(model "{esc}"',
            f"\t\t(offset",
            f"\t\t\t(xyz {offset[0]} {offset[1]} {offset[2]})",
            "\t\t)",
            f"\t\t(scale",
            f"\t\t\t(xyz {scale[0]} {scale[1]} {scale[2]})",
            "\t\t)",
            f"\t\t(rotate",
            f"\t\t\t(xyz {rotate[0]} {rotate[1]} {rotate[2]})",
            "\t\t)",
            "\t)",
        ]
    lines.append(")")
    return "\n".join(lines) + "\n"


def write_footprint(path: Path, name: Optional[str] = None, **kw) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = name if name is not None else path.stem
    path.write_text(footprint_text(name, **kw), encoding="utf-8", newline="\n")
    return path


# --------------------------------------------------------------------------
# 3D models
# --------------------------------------------------------------------------

def write_step(path: Path, *, crlf: bool = False) -> Path:
    """
    Tiny but structurally plausible ASCII STEP. Vendors ship these with CRLF,
    so that variant is available on purpose.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "ISO-10303-21;\n"
        "HEADER;\n"
        "FILE_DESCRIPTION(('STEP AP214'),'1');\n"
        f"FILE_NAME('{path.name}','2026-10-04T00:00:00',('test'),('test'),'','','');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\n"
        "ENDSEC;\n"
        "DATA;\n"
        "#1=CARTESIAN_POINT('',(0.,0.,0.));\n"
        "ENDSEC;\n"
        "END-ISO-10303-21;\n"
    )
    if crlf:
        path.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
    else:
        path.write_text(body, encoding="utf-8", newline="\n")
    return path


# --------------------------------------------------------------------------
# Vendor ZIP bundles
# --------------------------------------------------------------------------

def _zip(path: Path, members: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, data in members.items():
            zf.writestr(arcname, data)
    return path


def zip_snapeda(path: Path, part: str = "TPA3255DDV", footprint: str = "SOP63P810X120-44N") -> Path:
    """SnapEDA: everything flat at the archive root, plus a datasheet."""
    return _zip(path, {
        f"{part}.kicad_sym": symbol_lib([symbol_block(part, footprint)]),
        f"{footprint}.kicad_mod": footprint_text(footprint),
        f"{part}.step": "ISO-10303-21;\nEND-ISO-10303-21;\n",
        f"{part}.pdf": "%PDF-1.4 fake datasheet\n",
    })


def zip_ultra_librarian(path: Path, part: str = "LM358", footprint: str = "SOIC127P600X175-8N") -> Path:
    """Ultra Librarian: nested KiCAD/ and 3D/ folders under a versioned root."""
    return _zip(path, {
        f"ul_{part}/KiCAD/{part}.kicad_sym": symbol_lib([symbol_block(part, footprint)]),
        f"ul_{part}/KiCAD/{part}.kicad_mod": footprint_text(footprint),
        f"ul_{part}/3D/{footprint}.step": "ISO-10303-21;\nEND-ISO-10303-21;\n",
        f"ul_{part}/Readme.txt": "Generated by Ultra Librarian\n",
    })


def zip_component_search_engine(path: Path, part: str = "RP2040", footprint: str = "QFN50P700X700X90-57N") -> Path:
    """Component Search Engine / SamacSys: KiCad/ + 3D/ + html readme."""
    return _zip(path, {
        f"{part}/KiCad/{part}.kicad_sym": symbol_lib([symbol_block(part, footprint)]),
        f"{part}/KiCad/{footprint}.kicad_mod": footprint_text(footprint),
        f"{part}/3D/{footprint}.stp": "ISO-10303-21;\nEND-ISO-10303-21;\n",
        f"{part}/Readme.html": "<html>SamacSys</html>\n",
    })


def zip_with_macos_junk(path: Path, part: str = "NE555") -> Path:
    """
    A ZIP made on macOS. __MACOSX/._* members are AppleDouble resource forks,
    not KiCad files, and rglob() happily returns them.
    """
    return _zip(path, {
        f"{part}.kicad_sym": symbol_lib([symbol_block(part, "DIP254P762X508-8N")]),
        f"{part}.kicad_mod": footprint_text("DIP254P762X508-8N"),
        f"__MACOSX/._{part}.kicad_sym": "\x00\x05\x16\x07junk resource fork",
        f"__MACOSX/._{part}.kicad_mod": "\x00\x05\x16\x07junk resource fork",
        ".DS_Store": "\x00\x01junk",
    })


def zip_multi_part(path: Path) -> Path:
    """Three independent parts in one archive -- taking [0] silently loses two."""
    members = {}
    for part, fp in (("R_0603", "R_0603_1608Metric"),
                     ("C_0603", "C_0603_1608Metric"),
                     ("L_0805", "L_0805_2012Metric")):
        members[f"{part}/{part}.kicad_sym"] = symbol_lib([symbol_block(part, fp)])
        members[f"{part}/{fp}.kicad_mod"] = footprint_text(fp)
        members[f"{part}/{fp}.step"] = "ISO-10303-21;\nEND-ISO-10303-21;\n"
    return _zip(path, members)


def zip_multi_symbol_cache(path: Path) -> Path:
    """
    A project cache library masquerading as a part download -- this is literally
    what staging-temp/TPA3155DDV/TPA3255DDV.kicad_sym is. Note the hostile
    names: '*' is illegal on Windows, '(' breaks naive text parsing, and the
    Footprint properties point at a project nickname that does not exist here.
    """
    names = [
        "CONN-5MM-4P",
        "DC_Converter_V100-EY9",
        "Inductor_2*10uH_Leaded_7W15",
        "Mini-Fit(MX4.2)_HX-5569-2x2A",
        "TPA3255DDV",
    ]
    return _zip(path, {
        "TPA3255DDV.kicad_sym": symbol_lib([
            symbol_block(n, f"TPA3255_BTL_Mono:{n}") for n in names
        ]),
        "SOP63P810X120-44N.kicad_mod": footprint_text("SOP63P810X120-44N"),
        "TPA3255DDV.step": "ISO-10303-21;\nEND-ISO-10303-21;\n",
    })


def zip_unsafe_paths(path: Path) -> Path:
    """Zip-slip: members that escape the extraction root."""
    return _zip(path, {
        "../escaped.kicad_sym": symbol_lib([symbol_block("Escaped")]),
        "ok/Good.kicad_sym": symbol_lib([symbol_block("Good")]),
    })
