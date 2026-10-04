"""
Quote- and paren-aware reading, inspection and mutation of KiCad S-expressions.

Design rules, all of them load-bearing (see AGENTS.md Rule 1):

* Nothing here ever uses `re.sub` with an interpolated replacement string. A
  replacement containing a backslash -- a Windows path, an escaped quote --
  would otherwise be re-interpreted as a group reference and silently corrupt
  the file. All edits are computed as (start, end, text) slices and applied
  back-to-front.
* Every scan skips over quoted strings, so a '(' or ')' inside a value cannot
  throw off paren matching.
* Edits replace only the exact span of the token being changed, so UUIDs,
  float formatting, indentation, property order and unknown tokens survive
  untouched.
* Reads tolerate a BOM and CRLF; writes always emit LF. Reading a clean
  LF/no-BOM file and writing it back is byte-identical.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


class SExprError(ValueError):
    """Malformed S-expression."""


# --------------------------------------------------------------------------
# File I/O
# --------------------------------------------------------------------------

def read_text(path: Path) -> str:
    """Read a KiCad file as text: strip a BOM, normalise CRLF/CR to LF."""
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8-sig")
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def write_text(path: Path, text: str) -> None:
    """
    Write text with LF endings, atomically.

    The temp file is created in the destination directory so os.replace is a
    same-filesystem rename, which is atomic. A crash therefore leaves either
    the old file or the new one, never a half-written KiCad file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# String token handling
# --------------------------------------------------------------------------

def quote(value: str) -> str:
    """Render a Python string as a KiCad quoted token."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unquote(token: str) -> str:
    """Decode a KiCad quoted token (including the surrounding quotes)."""
    if len(token) < 2 or token[0] != '"' or token[-1] != '"':
        raise SExprError(f"not a quoted token: {token[:40]!r}")
    out: List[str] = []
    i = 1
    end = len(token) - 1
    while i < end:
        c = token[i]
        if c == "\\" and i + 1 < end:
            out.append(token[i + 1])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _skip_string(text: str, i: int) -> int:
    """Given the index of an opening '"', return the index just past its close."""
    i += 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    raise SExprError("unterminated string")


# --------------------------------------------------------------------------
# Paren navigation
# --------------------------------------------------------------------------

def find_matching_paren(text: str, start_index: int) -> int:
    """
    Index of the ')' matching the '(' at `start_index`.

    Backslash escapes are only honoured inside strings, which is where KiCad
    actually uses them; outside a string a backslash is an ordinary character.
    Returns -1 when unmatched, matching the previous API.
    """
    if start_index >= len(text) or text[start_index] != "(":
        return -1
    depth = 0
    i = start_index
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            try:
                i = _skip_string(text, i)
            except SExprError:
                return -1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def root_node(text: str) -> Tuple[int, int]:
    """(open, close) span of the outermost expression."""
    start = text.find("(")
    if start == -1:
        raise SExprError("no S-expression found")
    end = find_matching_paren(text, start)
    if end == -1:
        raise SExprError("unbalanced parentheses in root node")
    return start, end


def node_head(text: str, open_idx: int) -> str:
    """The bare symbol immediately after '(' , e.g. 'symbol', 'property', 'model'."""
    i = open_idx + 1
    n = len(text)
    while i < n and text[i] in " \t\n\r":
        i += 1
    j = i
    while j < n and text[j] not in " \t\n\r()\"":
        j += 1
    return text[i:j]


def children(text: str, open_idx: int) -> List[Tuple[str, int, int]]:
    """Direct child nodes of the node at `open_idx`, as (head, open, close)."""
    close = find_matching_paren(text, open_idx)
    if close == -1:
        raise SExprError("unbalanced parentheses")
    out: List[Tuple[str, int, int]] = []
    i = open_idx + 1
    while i < close:
        c = text[i]
        if c == '"':
            i = _skip_string(text, i)
            continue
        if c == "(":
            sub_close = find_matching_paren(text, i)
            if sub_close == -1:
                raise SExprError("unbalanced parentheses in child node")
            out.append((node_head(text, i), i, sub_close))
            i = sub_close + 1
            continue
        i += 1
    return out


def string_tokens(text: str, open_idx: int, limit: Optional[int] = None) -> List[Tuple[str, int, int]]:
    """
    Quoted tokens that belong directly to this node, as (value, start, end)
    where [start, end) covers the token *including* its quotes. Nested nodes
    are skipped, so `(property "Footprint" "Lib:FP" (at 0 0 0) ...)` yields
    exactly the two strings and not whatever lives inside (at ...).
    """
    close = find_matching_paren(text, open_idx)
    if close == -1:
        raise SExprError("unbalanced parentheses")
    out: List[Tuple[str, int, int]] = []
    i = open_idx + 1
    while i < close:
        c = text[i]
        if c == "(":
            sub = find_matching_paren(text, i)
            if sub == -1:
                raise SExprError("unbalanced parentheses")
            i = sub + 1
            continue
        if c == '"':
            end = _skip_string(text, i)
            out.append((unquote(text[i:end]), i, end))
            if limit is not None and len(out) >= limit:
                return out
            i = end
            continue
        i += 1
    return out


def node_name(text: str, open_idx: int) -> Optional[str]:
    """The first quoted token of a node -- its name for symbol/footprint/model."""
    toks = string_tokens(text, open_idx, limit=1)
    return toks[0][0] if toks else None


# --------------------------------------------------------------------------
# Edit application
# --------------------------------------------------------------------------

def apply_edits(text: str, edits: Sequence[Tuple[int, int, str]]) -> str:
    """
    Apply (start, end, replacement) slice edits.

    Applied in descending start order so earlier offsets stay valid, and
    overlapping edits are rejected rather than silently producing garbage.
    """
    ordered = sorted(edits, key=lambda e: (e[0], e[1]), reverse=True)
    last_start = len(text) + 1
    for start, end, _ in ordered:
        if start < 0 or end > len(text) or start > end:
            raise SExprError(f"edit span out of range: {(start, end)}")
        if end > last_start:
            raise SExprError(f"overlapping edits at {(start, end)}")
        last_start = start
    out = text
    for start, end, replacement in ordered:
        out = out[:start] + replacement + out[end:]
    return out


# --------------------------------------------------------------------------
# Symbols
# --------------------------------------------------------------------------

def top_level_symbols(text: str) -> List[Tuple[str, int, int]]:
    """
    Every top-level (symbol "NAME" ...) in a .kicad_sym, as (name, open, close).

    Official libraries always hold exactly one (AGENTS.md 6.1); more than one
    means a vendor bundle or a project cache library that ingest must split.
    """
    r_open, _ = root_node(text)
    out = []
    for head, open_idx, close_idx in children(text, r_open):
        if head != "symbol":
            continue
        name = node_name(text, open_idx)
        if name is not None:
            out.append((name, open_idx, close_idx))
    return out


def find_symbol(text: str, name: str) -> Optional[Tuple[int, int]]:
    """(open, close) span of the top-level symbol called `name`."""
    for n, open_idx, close_idx in top_level_symbols(text):
        if n == name:
            return open_idx, close_idx
    return None


def unit_subsymbols(text: str, symbol_open: int) -> List[Tuple[str, int, int]]:
    """Nested (symbol "<Parent>_<unit>_<style>" ...) blocks of a symbol."""
    return [
        (node_name(text, o) or "", o, c)
        for head, o, c in children(text, symbol_open)
        if head == "symbol"
    ]


def extends_target(text: str, symbol_open: int) -> Optional[Tuple[str, int, int]]:
    """
    The (extends "PARENT") of a derived symbol, as (parent, start, end) over
    the quoted token. The parent always lives in a *sibling file* of the same
    .kicad_symdir -- see AGENTS.md 6.2.
    """
    for head, o, _c in children(text, symbol_open):
        if head == "extends":
            toks = string_tokens(text, o, limit=1)
            if toks:
                value, start, end = toks[0]
                return value, start, end
    return None


# --------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------

def _property_nodes(text: str, block_open: int) -> List[Tuple[str, int, int]]:
    out = []
    for head, o, c in children(text, block_open):
        if head == "property":
            toks = string_tokens(text, o, limit=1)
            if toks:
                out.append((toks[0][0], o, c))
    return out


def get_property(text: str, block_open: int, name: str) -> Optional[str]:
    """
    Value of `(property "<name>" "<value>" ...)` within one specific block.

    Scoped to `block_open` on purpose: a multi-symbol file has one Footprint
    property per symbol, and the old code always patched the first one.
    """
    for pname, o, _c in _property_nodes(text, block_open):
        if pname == name:
            toks = string_tokens(text, o, limit=2)
            if len(toks) >= 2:
                return toks[1][0]
            return ""
    return None


def set_property_edits(
    text: str, block_open: int, name: str, value: str
) -> List[Tuple[int, int, str]]:
    """Edits that set a property's value in one block. Empty if absent."""
    for pname, o, _c in _property_nodes(text, block_open):
        if pname != name:
            continue
        toks = string_tokens(text, o, limit=2)
        if len(toks) >= 2:
            _v, start, end = toks[1]
            return [(start, end, quote(value))]
        # Property with a name but no value token: insert one after the name.
        _n, _s, nend = toks[0]
        return [(nend, nend, " " + quote(value))]
    return []


def set_property(text: str, block_open: int, name: str, value: str) -> str:
    edits = set_property_edits(text, block_open, name, value)
    return apply_edits(text, edits) if edits else text


def set_symbol_footprint(text: str, symbol_name: str, footprint_id: str) -> str:
    """Set one named symbol's Footprint property to '<Category>:<Footprint>'."""
    span = find_symbol(text, symbol_name)
    if span is None:
        raise SExprError(f"symbol {symbol_name!r} not found")
    return set_property(text, span[0], "Footprint", footprint_id)


# --------------------------------------------------------------------------
# Renaming
# --------------------------------------------------------------------------

def rename_symbol(text: str, old: str, new: str) -> str:
    """
    Rename one top-level symbol within a single file.

    Rewrites, in this order of discovery: the top-level name token, every
    unit sub-symbol named '<old>_<unit>_<style>', and the Value property when
    it still equals `old`. Everything else -- UUIDs, graphics, pins, other
    properties -- is left byte-identical.

    `(extends "old")` references live in *sibling files*, so fixing those is
    retarget_extends()'s job, called by the refactor layer across the symdir.
    A same-file extends is also handled here, defensively.
    """
    span = find_symbol(text, old)
    if span is None:
        raise SExprError(f"symbol {old!r} not found")
    open_idx, _close = span

    edits: List[Tuple[int, int, str]] = []

    name_tok = string_tokens(text, open_idx, limit=1)[0]
    edits.append((name_tok[1], name_tok[2], quote(new)))

    prefix = f"{old}_"
    for uname, uopen, _uclose in unit_subsymbols(text, open_idx):
        if uname == old or uname.startswith(prefix):
            suffix = uname[len(old):]
            tok = string_tokens(text, uopen, limit=1)[0]
            edits.append((tok[1], tok[2], quote(new + suffix)))

    if get_property(text, open_idx, "Value") == old:
        edits += set_property_edits(text, open_idx, "Value", new)

    for _n, other_open, _c in top_level_symbols(text):
        target = extends_target(text, other_open)
        if target and target[0] == old:
            edits.append((target[1], target[2], quote(new)))

    return apply_edits(text, edits)


def retarget_extends(text: str, old_parent: str, new_parent: str) -> Tuple[str, int]:
    """
    Point every `(extends "old_parent")` in this file at `new_parent`.

    Used on each sibling file of a symdir when its parent symbol is renamed.
    Returns the new text and how many references changed, so the caller can
    report counts and skip untouched files.
    """
    edits = []
    for _n, open_idx, _c in top_level_symbols(text):
        target = extends_target(text, open_idx)
        if target and target[0] == old_parent:
            edits.append((target[1], target[2], quote(new_parent)))
    if not edits:
        return text, 0
    return apply_edits(text, edits), len(edits)


def extract_symbol_file(text: str, name: str) -> str:
    """
    Build a single-symbol .kicad_sym holding just `name`.

    Used to split a multi-symbol vendor bundle or project cache library into
    the one-symbol-per-file form every official library uses (AGENTS.md 6.1).
    The library envelope -- version, generator, generator_version -- is
    carried over verbatim, and the symbol block is copied byte for byte, so
    pins, graphics and UUIDs are untouched. Top-level symbols are indented one
    tab both before and after the split, so the inner indentation stays valid.
    """
    r_open, _r_close = root_node(text)
    head = node_head(text, r_open)
    span = find_symbol(text, name)
    if span is None:
        raise SExprError(f"symbol {name!r} not found")

    envelope = [
        text[o : c + 1]
        for h, o, c in children(text, r_open)
        if h != "symbol"
    ]
    block = text[span[0] : span[1] + 1]

    lines = [f"({head}"]
    lines += [f"\t{item}" for item in envelope]
    lines.append(f"\t{block}")
    lines.append(")")
    return "\n".join(lines) + "\n"


def footprint_span(text: str) -> Tuple[str, int, int]:
    """(name, open, close) of the single (footprint "NAME" ...) in a .kicad_mod."""
    open_idx, close_idx = root_node(text)
    if node_head(text, open_idx) != "footprint":
        raise SExprError("not a footprint file")
    name = node_name(text, open_idx)
    if name is None:
        raise SExprError("footprint has no name")
    return name, open_idx, close_idx


def rename_footprint(text: str, old: str, new: str) -> str:
    """Rename the footprint declaration itself."""
    name, open_idx, _ = footprint_span(text)
    if name != old:
        raise SExprError(f"footprint is named {name!r}, not {old!r}")
    tok = string_tokens(text, open_idx, limit=1)[0]
    return apply_edits(text, [(tok[1], tok[2], quote(new))])


# --------------------------------------------------------------------------
# 3D models
# --------------------------------------------------------------------------

@dataclass
class ModelRef:
    """One (model ...) block of a footprint."""
    path: str
    open: int
    close: int
    path_start: int
    path_end: int
    offset: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    rotate: Tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def filename(self) -> str:
        """Basename of the model path, separator-agnostic."""
        return self.path.replace("\\", "/").rsplit("/", 1)[-1]


def _xyz(text: str, block_open: int, head: str, default) -> Tuple[float, float, float]:
    for h, o, _c in children(text, block_open):
        if h != head:
            continue
        for h2, o2, c2 in children(text, o):
            if h2 == "xyz":
                parts = text[o2 + 1 : c2].split()[1:]
                try:
                    return (float(parts[0]), float(parts[1]), float(parts[2]))
                except (IndexError, ValueError):
                    return default
        # KiCad also accepts the flat form (offset (xyz ...)) vs (offset x y z)
        parts = text[o + 1 : _c].split()[1:]
        try:
            return (float(parts[0]), float(parts[1]), float(parts[2]))
        except (IndexError, ValueError):
            return default
    return default


def all_models(text: str) -> List[ModelRef]:
    """
    Every (model ...) block of a footprint, in file order.

    A footprint may legitimately have zero models (the staged
    SOP63P810X120-44N.kicad_mod has none) or several (a connector with a
    separate mating part), so callers must not assume index 0 exists.
    """
    _name, open_idx, _close = footprint_span(text)
    out: List[ModelRef] = []
    for head, o, c in children(text, open_idx):
        if head != "model":
            continue
        toks = string_tokens(text, o, limit=1)
        if not toks:
            continue
        path, pstart, pend = toks[0]
        out.append(
            ModelRef(
                path=path, open=o, close=c, path_start=pstart, path_end=pend,
                offset=_xyz(text, o, "offset", (0.0, 0.0, 0.0)),
                scale=_xyz(text, o, "scale", (1.0, 1.0, 1.0)),
                rotate=_xyz(text, o, "rotate", (0.0, 0.0, 0.0)),
            )
        )
    return out


def set_model_path(text: str, index: int, new_path: str) -> str:
    """
    Repoint one model's path, leaving its offset/scale/rotate alone.

    Only the quoted path token is replaced, so a hand-tuned 3D alignment
    survives a category move -- which is the whole point of storing the
    offsets in the footprint.
    """
    models = all_models(text)
    if not -len(models) <= index < len(models):
        raise SExprError(f"model index {index} out of range ({len(models)} models)")
    m = models[index]
    return apply_edits(text, [(m.path_start, m.path_end, quote(new_path))])


def add_model(
    text: str,
    path: str,
    *,
    offset: Tuple[float, float, float] = (0.0, 0.0, 0.0),
    scale: Tuple[float, float, float] = (1.0, 1.0, 1.0),
    rotate: Tuple[float, float, float] = (0.0, 0.0, 0.0),
    indent: str = "\t",
) -> str:
    """
    Append a (model ...) block just inside the footprint's closing paren.

    Needed because vendors ship footprints with no model block at all, in
    which case there is no path to repoint -- one has to be created.
    """
    _name, _open, close = footprint_span(text)
    block = (
        f"{indent}(model {quote(path)}\n"
        f"{indent}\t(offset\n{indent}\t\t(xyz {offset[0]} {offset[1]} {offset[2]})\n{indent}\t)\n"
        f"{indent}\t(scale\n{indent}\t\t(xyz {scale[0]} {scale[1]} {scale[2]})\n{indent}\t)\n"
        f"{indent}\t(rotate\n{indent}\t\t(xyz {rotate[0]} {rotate[1]} {rotate[2]})\n{indent}\t)\n"
        f"{indent})\n"
    )
    # Insert before the final ')', preserving whatever whitespace precedes it.
    insert_at = close
    while insert_at > 0 and text[insert_at - 1] in " \t":
        insert_at -= 1
    if insert_at > 0 and text[insert_at - 1] != "\n":
        block = "\n" + block
    return apply_edits(text, [(insert_at, insert_at, block)])


def set_or_add_model(text: str, path: str, **kw) -> str:
    """Repoint the first model if there is one, otherwise create it."""
    return set_model_path(text, 0, path) if all_models(text) else add_model(text, path, **kw)


# --------------------------------------------------------------------------
# Backwards-compatible helpers used by the not-yet-rewritten callers
# --------------------------------------------------------------------------

def get_symbol_footprint_id(kicad_sym_path: Path) -> Optional[str]:
    """Footprint property of the first top-level symbol, or None."""
    try:
        text = read_text(Path(kicad_sym_path))
        syms = top_level_symbols(text)
    except (OSError, SExprError):
        return None
    if not syms:
        return None
    return get_property(text, syms[0][1], "Footprint")


# --------------------------------------------------------------------------
# Deprecated path-based API
#
# Kept only so manifest.py and packager.py keep importing while Phase 1 lands.
# New code uses the text-in/text-out functions above together with ops.py,
# which plans every change before touching the disk. Delete these once the
# last caller is gone.
# --------------------------------------------------------------------------

def patch_footprint_3d_model(
    kicad_mod_path: Path,
    new_model_path: str,
    default_offset: Tuple[float, float, float] = (0.0, 0.0, 0.0),
    default_scale: Tuple[float, float, float] = (1.0, 1.0, 1.0),
    default_rotate: Tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> bool:
    """Deprecated. Repoint or create the first model block of a footprint file."""
    path = Path(kicad_mod_path)
    try:
        text = read_text(path)
        new_text = set_or_add_model(
            text, new_model_path,
            offset=default_offset, scale=default_scale, rotate=default_rotate,
        )
    except (OSError, SExprError):
        return False
    if new_text != text:
        write_text(path, new_text)
    return True


def patch_symbol_footprint(kicad_sym_path: Path, new_footprint_id: str) -> bool:
    """
    Deprecated. Set the Footprint property of *every* top-level symbol.

    The old implementation patched only the first match in the file, which
    silently mislabelled every other symbol in a multi-symbol bundle.
    """
    path = Path(kicad_sym_path)
    try:
        text = read_text(path)
        syms = top_level_symbols(text)
    except (OSError, SExprError):
        return False
    if not syms:
        return False
    edits: List[Tuple[int, int, str]] = []
    for _name, open_idx, _close in syms:
        edits += set_property_edits(text, open_idx, "Footprint", new_footprint_id)
    if not edits:
        return False
    new_text = apply_edits(text, edits)
    if new_text != text:
        write_text(path, new_text)
    return True


def extract_footprint_model_info(kicad_mod_path: Path) -> Optional[Dict[str, object]]:
    """Deprecated. First model block of a footprint file, or None."""
    try:
        text = read_text(Path(kicad_mod_path))
        models = all_models(text)
    except (OSError, SExprError):
        return None
    if not models:
        return None
    m = models[0]
    return {
        "raw_block": text[m.open : m.close + 1],
        "path": m.path,
        "start": m.open,
        "end": m.close,
    }
