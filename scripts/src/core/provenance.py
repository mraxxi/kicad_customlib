"""
provenance.json -- the small amount of metadata the disk cannot tell us.

This replaces manifest.json. The old manifest tried to be a database of
"parts" and, because it was the source of truth, a stale or empty record made
the library look like it contained things it did not: the committed manifest
claimed a part 'SOP63P810X120-44N' in category '3255' with `files: {}`, and
nothing on disk ever backed that up.

What survives here is only what scanning cannot recover: where a file
originally came from, what it was called at the vendor, and when it was
imported. No global last_updated (it made every operation a git conflict
between the two machines) and no history array (git is the history).

Missing provenance is never an error -- the library works perfectly without
it. Corrupt provenance is always an error, and never silently reset: the old
_load_manifest() swallowed JSON errors and returned an empty manifest, so the
next save() wiped the file.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional

FILENAME = "provenance.json"
VERSION = 2

KIND_SYMBOL = "symbol"
KIND_FOOTPRINT = "footprint"
KIND_MODEL = "model"
KINDS = (KIND_SYMBOL, KIND_FOOTPRINT, KIND_MODEL)


class ProvenanceError(RuntimeError):
    """provenance.json exists but cannot be understood."""


def make_key(category: str, kind: str, name: str) -> str:
    """
    '<Category>/<kind>/<name>'.

    Keying by category as well as name is deliberate: the old manifest was
    keyed on the bare part name, so the same part name in two categories
    overwrote itself.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    return f"{category}/{kind}/{name}"


def split_key(key: str) -> tuple[str, str, str]:
    parts = key.split("/")
    if len(parts) != 3:
        raise ValueError(f"malformed provenance key {key!r}")
    return parts[0], parts[1], parts[2]


@dataclass
class Item:
    """Provenance for one file."""
    original_name: str = ""
    source: str = ""
    imported: str = ""          # ISO date, no time -- a timestamp is churn

    def to_json(self) -> Dict[str, str]:
        return {k: v for k, v in (
            ("original_name", self.original_name),
            ("source", self.source),
            ("imported", self.imported),
        ) if v}

    @classmethod
    def from_json(cls, raw: object, key: str) -> "Item":
        if not isinstance(raw, dict):
            raise ProvenanceError(f"entry {key!r} is {type(raw).__name__}, expected an object")
        return cls(
            original_name=str(raw.get("original_name", "")),
            source=str(raw.get("source", "")),
            imported=str(raw.get("imported", "")),
        )


@dataclass
class Provenance:
    path: Path
    items: Dict[str, Item] = field(default_factory=dict)
    existed: bool = False

    # -- access -----------------------------------------------------------
    def get(self, category: str, kind: str, name: str) -> Optional[Item]:
        return self.items.get(make_key(category, kind, name))

    def record(
        self,
        category: str,
        kind: str,
        name: str,
        *,
        original_name: str = "",
        source: str = "",
        imported: Optional[str] = None,
    ) -> Item:
        item = Item(
            original_name=original_name,
            source=source,
            imported=imported or date.today().isoformat(),
        )
        self.items[make_key(category, kind, name)] = item
        return item

    def forget(self, category: str, kind: str, name: str) -> bool:
        return self.items.pop(make_key(category, kind, name), None) is not None

    def rename(
        self, category: str, kind: str, old: str, new: str, *, new_category: Optional[str] = None
    ) -> bool:
        """Follow a rename or a move. Absent provenance is not an error."""
        item = self.items.pop(make_key(category, kind, old), None)
        if item is None:
            return False
        self.items[make_key(new_category or category, kind, new)] = item
        return True

    def keys_for_category(self, category: str) -> List[str]:
        prefix = f"{category}/"
        return sorted(k for k in self.items if k.startswith(prefix))

    def rename_category(self, old: str, new: str) -> int:
        moved = 0
        for key in self.keys_for_category(old):
            _cat, kind, name = split_key(key)
            self.items[make_key(new, kind, name)] = self.items.pop(key)
            moved += 1
        return moved

    # -- serialisation ----------------------------------------------------
    def to_json_text(self) -> str:
        """Sorted keys, indent 2, trailing newline -- a stable diff."""
        payload = {
            "version": VERSION,
            "items": {k: self.items[k].to_json() for k in sorted(self.items)},
        }
        return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    def save(self, *, backup: bool = True) -> Path:
        """
        Write atomically, backing up any existing file first.

        The backup is a safety net for the case this module is wrong about
        something; it is gitignored and local to the machine.
        """
        from . import s_expr as sx  # atomic write_text

        if backup and self.path.exists():
            shutil.copy2(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
        sx.write_text(self.path, self.to_json_text())
        self.existed = True
        return self.path


def load(root: Path, *, filename: str = FILENAME) -> Provenance:
    """
    Read provenance.json.

    A missing file yields an empty Provenance. A malformed one raises
    ProvenanceError with the parse position, and the file is left untouched.
    """
    path = Path(root) / filename
    if not path.exists():
        return Provenance(path=path)

    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ProvenanceError(f"cannot read {path}: {exc}") from exc

    if not raw_text.strip():
        raise ProvenanceError(
            f"{path} is empty; delete it to start fresh, or restore it from git"
        )

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ProvenanceError(
            f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}: {exc.msg}). "
            f"The file has not been modified; fix or delete it, or restore it from git."
        ) from exc

    if not isinstance(data, dict):
        raise ProvenanceError(f"{path}: expected an object at the top level")

    version = data.get("version")
    if version != VERSION:
        raise ProvenanceError(
            f"{path}: version {version!r}, expected {VERSION}. "
            f"Run 'lib_manager.py migrate-manifest' if this is an old manifest."
        )

    raw_items = data.get("items", {})
    if not isinstance(raw_items, dict):
        raise ProvenanceError(f"{path}: 'items' must be an object")

    prov = Provenance(path=path, existed=True)
    for key, value in raw_items.items():
        split_key(key)  # validates the shape
        prov.items[key] = Item.from_json(value, key)
    return prov


# --------------------------------------------------------------------------
# Deferred edits
# --------------------------------------------------------------------------

def apply_edits(prov: Provenance, edits: Sequence["object"]) -> int:
    """
    Run the provenance edits a plan recorded, after its operations succeeded.

    Callers must invoke this only once, and only on success. Building a plan
    deliberately does not touch provenance -- see ops.ProvenanceEdit for why.
    Returns the number of edits that changed something.
    """
    applied = 0
    for edit in edits:
        action = getattr(edit, "action", None)
        if action == "record":
            prov.record(
                edit.category, edit.kind, edit.name,
                original_name=edit.original_name, source=edit.source,
            )
            applied += 1
        elif action == "rename":
            if prov.rename(
                edit.category, edit.kind, edit.name, edit.new_name,
                new_category=edit.new_category or None,
            ):
                applied += 1
        elif action == "forget":
            if prov.forget(edit.category, edit.kind, edit.name):
                applied += 1
        elif action == "rename_category":
            applied += prov.rename_category(edit.name, edit.new_name)
        else:
            raise ValueError(f"unknown provenance edit action {action!r}")
    return applied


# --------------------------------------------------------------------------
# Pruning
# --------------------------------------------------------------------------

def stale_keys(prov: Provenance, lib) -> List[str]:
    """
    Keys whose item is not on disk.

    These are harmless but they accumulate: nothing has ever removed them, so
    a library that has been reorganised collects an entry per abandoned name.
    `lib` is a library.Library; typed loosely to keep this module free of a
    scanner import.
    """
    out: List[str] = []
    for key in sorted(prov.items):
        category, kind, name = split_key(key)
        if kind == KIND_SYMBOL:
            found = lib.find_symbol(category, name)
        elif kind == KIND_FOOTPRINT:
            found = lib.find_footprint(category, name)
        else:
            found = lib.find_model(category, name)
        if found is None:
            out.append(key)
    return out


def prune(prov: Provenance, lib) -> List[str]:
    """Drop every stale entry. Returns the keys removed."""
    removed = stale_keys(prov, lib)
    for key in removed:
        prov.items.pop(key, None)
    return removed


# --------------------------------------------------------------------------
# One-time migration from manifest.json
# --------------------------------------------------------------------------

@dataclass
class MigrationReport:
    migrated: List[str] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)   # records with no files on disk
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"migrated {len(self.migrated)} entr{'y' if len(self.migrated) == 1 else 'ies'}"]
        for k in self.migrated:
            lines.append(f"  + {k}")
        for k in self.dropped:
            lines.append(f"  - dropped {k} (nothing on disk backs it up)")
        for w in self.warnings:
            lines.append(f"  warning: {w}")
        return "\n".join(lines)


def migrate_manifest(root: Path, *, manifest_name: str = "manifest.json") -> tuple[Provenance, MigrationReport]:
    """
    Convert manifest.json into provenance.json.

    Records whose files do not exist on disk are dropped rather than carried
    over -- they are what made the old manifest misleading. The manifest file
    itself is left in place for the owner to delete.
    """
    root = Path(root)
    report = MigrationReport()
    prov = load(root)

    manifest_path = root / manifest_name
    if not manifest_path.exists():
        report.warnings.append(f"{manifest_name} not found; nothing to migrate")
        return prov, report

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"cannot read {manifest_path}: {exc}") from exc

    parts = data.get("parts", {}) if isinstance(data, dict) else {}
    if not isinstance(parts, dict):
        raise ProvenanceError(f"{manifest_path}: 'parts' must be an object")

    for part_name, record in sorted(parts.items()):
        if not isinstance(record, dict):
            report.warnings.append(f"part {part_name!r} is not an object; skipped")
            continue

        category = str(record.get("category", "")).strip()
        files = record.get("files") or {}
        imported = str(record.get("import_date", ""))[:10]
        source = str(record.get("source_meta", "")) or str(record.get("original_import_name", ""))
        original = str(record.get("original_import_name", ""))

        if not category or not isinstance(files, dict) or not files:
            report.dropped.append(f"{category or '?'}/{part_name}")
            continue

        kind_for = {"symbol": KIND_SYMBOL, "footprint": KIND_FOOTPRINT, "model_3d": KIND_MODEL}
        recorded_any = False
        for file_key, rel_path in files.items():
            kind = kind_for.get(file_key)
            if kind is None:
                continue
            abs_path = root / str(rel_path)
            if not abs_path.exists():
                report.dropped.append(f"{category}/{kind}/{Path(str(rel_path)).stem}")
                continue
            name = abs_path.name if kind == KIND_MODEL else abs_path.stem
            key = make_key(category, kind, name)
            prov.items[key] = Item(
                original_name=original, source=source, imported=imported
            )
            report.migrated.append(key)
            recorded_any = True

        if not recorded_any and f"{category}/{part_name}" not in report.dropped:
            report.dropped.append(f"{category}/{part_name}")

    return prov, report
