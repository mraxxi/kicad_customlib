"""Phase 1: provenance.json -- metadata the disk cannot provide."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core import provenance as pv
from src.core.provenance import ProvenanceError
from tests import kicad_fixtures as kf


# --------------------------------------------------------------------------
# Absence is fine
# --------------------------------------------------------------------------

def test_missing_provenance_is_not_an_error(tmp_path):
    prov = pv.load(tmp_path)
    assert prov.items == {} and prov.existed is False


def test_a_library_with_no_provenance_still_reports_nothing_for_an_item(tmp_path):
    assert pv.load(tmp_path).get("Amp", pv.KIND_SYMBOL, "TPA3255DDV") is None


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

def test_keys_include_the_category_so_names_do_not_collide_across_categories(tmp_path):
    """The old manifest keyed on the bare part name and overwrote itself."""
    prov = pv.load(tmp_path)
    prov.record("Amp", pv.KIND_SYMBOL, "SHARED", source="a")
    prov.record("Conn", pv.KIND_SYMBOL, "SHARED", source="b")
    assert prov.get("Amp", pv.KIND_SYMBOL, "SHARED").source == "a"
    assert prov.get("Conn", pv.KIND_SYMBOL, "SHARED").source == "b"


def test_make_key_rejects_an_unknown_kind():
    with pytest.raises(ValueError, match="unknown kind"):
        pv.make_key("Amp", "gerber", "X")


def test_split_key_rejects_a_malformed_key():
    with pytest.raises(ValueError, match="malformed"):
        pv.split_key("Amp/symbol")


# --------------------------------------------------------------------------
# Round trip and stable output
# --------------------------------------------------------------------------

def test_save_and_load_round_trip(tmp_path):
    prov = pv.load(tmp_path)
    prov.record("Amp", pv.KIND_SYMBOL, "TPA3255DDV",
                original_name="TPA3255DDV.kicad_sym", source="SnapEDA zip",
                imported="2026-10-04")
    prov.save()

    again = pv.load(tmp_path)
    item = again.get("Amp", pv.KIND_SYMBOL, "TPA3255DDV")
    assert (item.original_name, item.source, item.imported) == (
        "TPA3255DDV.kicad_sym", "SnapEDA zip", "2026-10-04",
    )
    assert again.existed is True


def test_output_is_sorted_indented_and_newline_terminated(tmp_path):
    prov = pv.load(tmp_path)
    for name in ("Zeta", "Alpha", "Mu"):
        prov.record("Cat", pv.KIND_SYMBOL, name)
    text = prov.to_json_text()
    assert text.endswith("}\n")
    assert '\n  "items"' in text
    keys = list(json.loads(text)["items"])
    assert keys == sorted(keys)


def test_saved_file_uses_lf_and_has_no_global_timestamp(tmp_path):
    """
    No last_updated field: it made every operation a git conflict between the
    two machines even when the content was identical.
    """
    prov = pv.load(tmp_path)
    prov.record("Cat", pv.KIND_SYMBOL, "X")
    path = prov.save()
    raw = path.read_bytes()
    assert b"\r" not in raw
    data = json.loads(raw)
    assert set(data) == {"version", "items"}
    assert data["version"] == 2


def test_saving_twice_with_no_changes_produces_identical_bytes(tmp_path):
    prov = pv.load(tmp_path)
    prov.record("Cat", pv.KIND_SYMBOL, "X", imported="2026-10-04")
    first = prov.save().read_bytes()
    assert pv.load(tmp_path).save(backup=False).read_bytes() == first


def test_save_backs_up_the_previous_file(tmp_path):
    prov = pv.load(tmp_path)
    prov.record("Cat", pv.KIND_SYMBOL, "X")
    prov.save()
    prov.record("Cat", pv.KIND_SYMBOL, "Y")
    prov.save()
    backup = tmp_path / "provenance.json.bak"
    assert backup.exists()
    assert "Cat/symbol/Y" not in backup.read_text()


# --------------------------------------------------------------------------
# Corruption is loud, never silent
# --------------------------------------------------------------------------

def test_corrupt_json_raises_and_leaves_the_file_untouched(tmp_path):
    path = tmp_path / "provenance.json"
    broken = '{"version": 2, "items": {'
    path.write_text(broken)
    with pytest.raises(ProvenanceError, match="not valid JSON"):
        pv.load(tmp_path)
    assert path.read_text() == broken


def test_an_empty_file_raises_rather_than_being_treated_as_no_data(tmp_path):
    (tmp_path / "provenance.json").write_text("")
    with pytest.raises(ProvenanceError, match="is empty"):
        pv.load(tmp_path)


def test_a_wrong_version_points_at_the_migration_command(tmp_path):
    (tmp_path / "provenance.json").write_text('{"version": 1, "items": {}}')
    with pytest.raises(ProvenanceError, match="migrate-manifest"):
        pv.load(tmp_path)


def test_a_non_object_entry_is_rejected(tmp_path):
    (tmp_path / "provenance.json").write_text('{"version": 2, "items": {"A/symbol/X": 5}}')
    with pytest.raises(ProvenanceError, match="expected an object"):
        pv.load(tmp_path)


def test_error_message_names_the_parse_position(tmp_path):
    (tmp_path / "provenance.json").write_text('{\n "version": 2,\n "items": {oops}\n}')
    with pytest.raises(ProvenanceError, match=r"line 3"):
        pv.load(tmp_path)


# --------------------------------------------------------------------------
# Renames and moves
# --------------------------------------------------------------------------

def test_rename_moves_the_entry_to_the_new_key(tmp_path):
    prov = pv.load(tmp_path)
    prov.record("Amp", pv.KIND_SYMBOL, "OLD", source="s")
    assert prov.rename("Amp", pv.KIND_SYMBOL, "OLD", "NEW") is True
    assert prov.get("Amp", pv.KIND_SYMBOL, "OLD") is None
    assert prov.get("Amp", pv.KIND_SYMBOL, "NEW").source == "s"


def test_rename_can_also_change_the_category(tmp_path):
    prov = pv.load(tmp_path)
    prov.record("Old_Cat", pv.KIND_FOOTPRINT, "FP", source="s")
    prov.rename("Old_Cat", pv.KIND_FOOTPRINT, "FP", "FP", new_category="New_Cat")
    assert prov.get("New_Cat", pv.KIND_FOOTPRINT, "FP").source == "s"


def test_renaming_an_unknown_item_is_not_an_error(tmp_path):
    assert pv.load(tmp_path).rename("A", pv.KIND_SYMBOL, "nope", "x") is False


def test_rename_category_moves_every_entry(tmp_path):
    prov = pv.load(tmp_path)
    prov.record("Old", pv.KIND_SYMBOL, "S")
    prov.record("Old", pv.KIND_FOOTPRINT, "F")
    prov.record("Keep", pv.KIND_SYMBOL, "K")
    assert prov.rename_category("Old", "New") == 2
    assert prov.keys_for_category("New") == ["New/footprint/F", "New/symbol/S"]
    assert prov.keys_for_category("Old") == []
    assert prov.keys_for_category("Keep") == ["Keep/symbol/K"]


def test_forget_removes_an_entry(tmp_path):
    prov = pv.load(tmp_path)
    prov.record("A", pv.KIND_SYMBOL, "X")
    assert prov.forget("A", pv.KIND_SYMBOL, "X") is True
    assert prov.forget("A", pv.KIND_SYMBOL, "X") is False


# --------------------------------------------------------------------------
# Migration from manifest.json
# --------------------------------------------------------------------------

def _write_manifest(root: Path, parts: dict) -> None:
    (root / "manifest.json").write_text(json.dumps({
        "version": 1,
        "library_name": "KICAD_CUSTOM_LIB",
        "last_updated": "2026-10-04T15:07:47.666293+00:00",
        "parts": parts,
        "history": [{"action": "ingest", "part": "whatever"}],
    }, indent=2))


def test_migration_drops_records_that_nothing_on_disk_backs_up(tmp_path):
    """
    The real committed manifest: one part in category '3255' with files: {}.
    Carrying it over would reproduce the fiction that the library has content.
    """
    _write_manifest(tmp_path, {
        "SOP63P810X120-44N": {
            "display_name": "SOP63P810X120-44N",
            "category": "3255",
            "original_import_name": "SOP63P810X120-44N.kicad_mod",
            "import_date": "2026-10-04T15:07:47.666275+00:00",
            "source_meta": "",
            "files": {},
            "footprint_identifier": "",
        }
    })
    prov, report = pv.migrate_manifest(tmp_path)
    assert prov.items == {}
    assert report.migrated == []
    assert report.dropped == ["3255/SOP63P810X120-44N"]


def test_migration_keeps_records_whose_files_exist(tmp_path):
    kf.write_symbol(tmp_path / "symbols" / "Amp.kicad_symdir" / "TPA3255DDV.kicad_sym")
    kf.write_footprint(tmp_path / "footprints" / "Amp.pretty" / "SOP63.kicad_mod")
    kf.write_step(tmp_path / "3dmodels" / "Amp.3dshapes" / "TPA3255DDV.step")
    _write_manifest(tmp_path, {
        "TPA3255DDV": {
            "category": "Amp",
            "original_import_name": "tpa3255_snapeda.zip",
            "import_date": "2026-07-09T00:59:00+00:00",
            "source_meta": "SnapEDA",
            "files": {
                "symbol": "symbols/Amp.kicad_symdir/TPA3255DDV.kicad_sym",
                "footprint": "footprints/Amp.pretty/SOP63.kicad_mod",
                "model_3d": "3dmodels/Amp.3dshapes/TPA3255DDV.step",
            },
        }
    })
    prov, report = pv.migrate_manifest(tmp_path)
    assert sorted(prov.items) == [
        "Amp/footprint/SOP63", "Amp/model/TPA3255DDV.step", "Amp/symbol/TPA3255DDV",
    ]
    assert prov.get("Amp", pv.KIND_SYMBOL, "TPA3255DDV").source == "SnapEDA"
    assert prov.get("Amp", pv.KIND_SYMBOL, "TPA3255DDV").imported == "2026-07-09"
    assert report.dropped == []


def test_migration_drops_only_the_files_that_are_missing(tmp_path):
    kf.write_symbol(tmp_path / "symbols" / "Amp.kicad_symdir" / "S.kicad_sym")
    _write_manifest(tmp_path, {
        "S": {
            "category": "Amp",
            "files": {
                "symbol": "symbols/Amp.kicad_symdir/S.kicad_sym",
                "model_3d": "3dmodels/Amp.3dshapes/gone.step",
            },
        }
    })
    prov, report = pv.migrate_manifest(tmp_path)
    assert sorted(prov.items) == ["Amp/symbol/S"]
    assert report.dropped == ["Amp/model/gone"]


def test_migration_does_not_delete_the_manifest(tmp_path):
    _write_manifest(tmp_path, {})
    pv.migrate_manifest(tmp_path)
    assert (tmp_path / "manifest.json").exists()


def test_migration_without_a_manifest_warns_instead_of_failing(tmp_path):
    prov, report = pv.migrate_manifest(tmp_path)
    assert prov.items == {}
    assert any("not found" in w for w in report.warnings)


def test_migration_rejects_a_corrupt_manifest(tmp_path):
    (tmp_path / "manifest.json").write_text("{not json")
    with pytest.raises(ProvenanceError, match="cannot read"):
        pv.migrate_manifest(tmp_path)


def test_migration_does_not_carry_over_history_or_last_updated(tmp_path):
    kf.write_symbol(tmp_path / "symbols" / "Amp.kicad_symdir" / "S.kicad_sym")
    _write_manifest(tmp_path, {
        "S": {"category": "Amp", "files": {"symbol": "symbols/Amp.kicad_symdir/S.kicad_sym"}}
    })
    prov, _ = pv.migrate_manifest(tmp_path)
    data = json.loads(prov.to_json_text())
    assert set(data) == {"version", "items"}
