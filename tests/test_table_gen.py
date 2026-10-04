"""Phase 1: table_gen.py -- only real libraries get listed."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core import library as lb
from src.core import ops
from src.core import table_gen as tg
from tests import kicad_fixtures as kf


def _write(lib_root: Path, *, symbols=(), footprints=(), models=(), empty=()):
    for cat, name in symbols:
        kf.write_symbol(lib_root / "symbols" / f"{cat}.kicad_symdir" / f"{name}.kicad_sym")
    for cat, name in footprints:
        kf.write_footprint(lib_root / "footprints" / f"{cat}.pretty" / f"{name}.kicad_mod")
    for cat, name in models:
        kf.write_step(lib_root / "3dmodels" / f"{cat}.3dshapes" / name)
    for cat in empty:
        (lib_root / "symbols" / f"{cat}.kicad_symdir").mkdir(parents=True, exist_ok=True)
        (lib_root / "footprints" / f"{cat}.pretty").mkdir(parents=True, exist_ok=True)
        (lib_root / "3dmodels" / f"{cat}.3dshapes").mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Empty libraries must not be listed
# --------------------------------------------------------------------------

def test_empty_categories_are_omitted_from_both_tables(lib_root):
    """
    The committed sym-lib-table and fp-lib-table both advertise '3255' and
    'TI-TPAxxx_AUDIO-AMP', neither of which contains a single file. KiCad
    shows them as empty libraries, and since git cannot track an empty
    directory the other machine gets entries pointing at nothing.
    """
    _write(lib_root, empty=("3255", "TI-TPAxxx_AUDIO-AMP"))
    tables = tg.render_tables(lb.scan(lib_root))
    assert "3255" not in tables[tg.SYM_TABLE_NAME]
    assert "3255" not in tables[tg.FP_TABLE_NAME]
    assert tables[tg.SYM_TABLE_NAME] == '(sym_lib_table\n\t(version 7)\n)\n'


def test_a_category_is_listed_in_the_symbol_table_only_when_it_has_symbols(lib_root):
    _write(lib_root, symbols=[("HasSym", "S")], footprints=[("HasFp", "F")])
    tables = tg.render_tables(lb.scan(lib_root))
    assert "HasSym" in tables[tg.SYM_TABLE_NAME]
    assert "HasFp" not in tables[tg.SYM_TABLE_NAME]
    assert "HasFp" in tables[tg.FP_TABLE_NAME]
    assert "HasSym" not in tables[tg.FP_TABLE_NAME]


def test_a_category_with_only_a_3d_model_appears_in_neither_table(lib_root):
    """There is no table for 3D models -- they are reached via footprints."""
    _write(lib_root, models=[("ModelsOnly", "m.step")])
    tables = tg.render_tables(lb.scan(lib_root))
    assert "ModelsOnly" not in tables[tg.SYM_TABLE_NAME]
    assert "ModelsOnly" not in tables[tg.FP_TABLE_NAME]


def test_skipped_empty_categories_are_reported(lib_root):
    _write(lib_root, symbols=[("Real", "S")], empty=("3255",))
    assert tg.skipped_empty_categories(lb.scan(lib_root)) == ["3255"]


# --------------------------------------------------------------------------
# Format
# --------------------------------------------------------------------------

def test_table_format_matches_kicad_v7(lib_root):
    _write(lib_root, symbols=[("Amp_Test", "S")])
    text = tg.render_tables(lb.scan(lib_root))[tg.SYM_TABLE_NAME]
    assert text.splitlines()[0] == "(sym_lib_table"
    assert text.splitlines()[1] == "\t(version 7)"
    assert '(type "KiCad")' in text
    assert '(uri "${KICAD_CUSTOM_LIB}/symbols/Amp_Test.kicad_symdir")' in text
    assert text.endswith(")\n")


def test_uris_use_the_environment_variable_never_an_absolute_path(lib_root):
    _write(lib_root, symbols=[("C", "S")], footprints=[("C", "F")])
    for text in tg.render_tables(lb.scan(lib_root)).values():
        assert str(lib_root) not in text
        assert "${KICAD_CUSTOM_LIB}" in text


def test_output_is_sorted_and_free_of_timestamps(lib_root):
    _write(lib_root, symbols=[("Zeta", "S"), ("alpha", "S"), ("Mu", "S")])
    text = tg.render_tables(lb.scan(lib_root))[tg.SYM_TABLE_NAME]
    names = [l.split('"')[1] for l in text.splitlines() if "(lib " in l]
    assert names == ["alpha", "Mu", "Zeta"]


def test_generation_is_reproducible(lib_root):
    _write(lib_root, symbols=[("C", "S")])
    assert tg.render_tables(lb.scan(lib_root)) == tg.render_tables(lb.scan(lib_root))


def test_names_are_quoted_through_the_escaping_helper(lib_root):
    """naming.py forbids quotes in a category, but rendering must still escape."""
    entry = tg.LibEntry(name='Odd"Name', uri="u", descr="d")
    assert r'(name "Odd\"Name")' in entry.render()


# --------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------

def test_is_stale_is_false_right_after_generating(lib_root):
    _write(lib_root, symbols=[("C", "S")], footprints=[("C", "F")])
    tg.generate_tables(lib_root)
    assert tg.is_stale(lib_root) is False
    assert all(d == [] for d in tg.diff_tables(lib_root).values())


def test_is_stale_is_true_when_a_table_is_missing(lib_root):
    _write(lib_root, symbols=[("C", "S")])
    assert tg.is_stale(lib_root) is True


def test_is_stale_becomes_true_after_a_symbol_is_added(lib_root):
    _write(lib_root, symbols=[("C", "S")])
    tg.generate_tables(lib_root)
    _write(lib_root, symbols=[("Added", "S2")])
    assert tg.is_stale(lib_root) is True


def test_diff_tables_shows_the_stale_entry_being_removed(lib_root):
    """Reproduces the real repo: tables listing two categories, disk empty."""
    _write(lib_root, empty=("3255", "TI-TPAxxx_AUDIO-AMP"))
    (lib_root / tg.SYM_TABLE_NAME).write_text(
        '(sym_lib_table\n\t(version 7)\n'
        '\t(lib (name "3255") (type "KiCad") (uri "${KICAD_CUSTOM_LIB}/symbols/3255.kicad_symdir") (options "") (descr "Custom symbol library 3255"))\n'
        ')\n'
    )
    diff = tg.diff_tables(lib_root)[tg.SYM_TABLE_NAME]
    assert any(l.startswith("-") and '"3255"' in l for l in diff)


def test_an_empty_library_still_produces_a_valid_table(lib_root):
    tg.generate_tables(lib_root)
    text = (lib_root / tg.SYM_TABLE_NAME).read_text()
    assert text == "(sym_lib_table\n\t(version 7)\n)\n"


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def test_plan_generate_is_empty_when_the_tables_are_current(lib_root):
    _write(lib_root, symbols=[("C", "S")])
    tg.generate_tables(lib_root)
    assert tg.plan_generate(lib_root).is_empty


def test_plan_generate_writes_only_the_table_that_changed(lib_root):
    """A new *category* with only a footprint touches just the fp table."""
    _write(lib_root, symbols=[("C", "S")], footprints=[("C", "F")])
    tg.generate_tables(lib_root)
    _write(lib_root, footprints=[("NewCat", "F2")])
    plan = tg.plan_generate(lib_root)
    assert [op.target.name for op in plan.operations] == [tg.FP_TABLE_NAME]


def test_adding_a_part_to_an_existing_category_does_not_change_the_tables(lib_root):
    """
    The tables list libraries, not parts, so they are only stale when a
    category appears or disappears. This is why staleness cannot be derived
    from file counts.
    """
    _write(lib_root, symbols=[("C", "S")], footprints=[("C", "F")])
    tg.generate_tables(lib_root)
    _write(lib_root, footprints=[("C", "F2")], symbols=[("C", "S2")])
    assert tg.is_stale(lib_root) is False
    assert tg.plan_generate(lib_root).is_empty


def test_plan_generate_notes_each_empty_category(lib_root):
    _write(lib_root, empty=("3255",))
    assert any("'3255'" in n for n in tg.plan_generate(lib_root).notes)


def test_plan_generate_warns_about_an_official_nickname_collision(lib_root):
    _write(lib_root, symbols=[("Amplifier_Audio", "S")])
    plan = tg.plan_generate(lib_root)
    assert any("official KiCad library" in w for w in plan.warnings)


def test_plan_generate_warns_about_case_only_category_duplicates(lib_root):
    _write(lib_root, symbols=[("Amp", "S")])
    (lib_root / "footprints" / "amp.pretty").mkdir(parents=True)
    kf.write_footprint(lib_root / "footprints" / "amp.pretty" / "F.kicad_mod")
    assert any("only by case" in w for w in tg.plan_generate(lib_root).warnings)


def test_tables_are_written_with_lf(lib_root):
    _write(lib_root, symbols=[("C", "S")])
    tg.generate_tables(lib_root)
    assert b"\r" not in (lib_root / tg.SYM_TABLE_NAME).read_bytes()


def test_generate_tables_returns_both_paths(lib_root):
    sym, fp = tg.generate_tables(lib_root)
    assert sym.name == tg.SYM_TABLE_NAME and fp.name == tg.FP_TABLE_NAME


def test_deprecated_scan_libraries_now_excludes_empty_libraries(lib_root):
    _write(lib_root, symbols=[("Real", "S")], empty=("3255",))
    out = tg.scan_libraries(lib_root)
    assert [n for n, _p, _u in out["symbols"]] == ["Real"]
    assert out["footprints"] == []
