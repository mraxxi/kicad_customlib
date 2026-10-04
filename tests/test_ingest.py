"""Phase 1: ingest.py -- detect everything, guess nothing silently."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.core import ingest
from src.core import library as lb
from src.core import ops
from src.core import provenance as pv
from src.core import s_expr as sx
from tests import kicad_fixtures as kf

CAT = "Amp_Test"


def _run(lib_root: Path, source: Path, category: str = CAT, **kw):
    """prepare + apply, returning (preview-ish record, result)."""
    prov = kw.pop("prov", None)
    with ingest.prepare(lib_root, source, category, **kw) as prev:
        result = ops.apply(prev.plan)
        # Provenance edits are recorded on the plan and applied by the caller
        # only after the operations succeed.
        if prov is not None and result.ok:
            pv.apply_edits(prov, prev.plan.provenance)
        return prev.candidates, prev.plan, result


def kinds(candidates, kind):
    return [c for c in candidates if c.kind == kind]


# --------------------------------------------------------------------------
# Detection finds everything
# --------------------------------------------------------------------------

def test_a_multi_part_zip_imports_every_part(tmp_path, lib_root):
    """The old code took [0] of each list and silently dropped the rest."""
    z = kf.zip_multi_part(tmp_path / "parts.zip")
    candidates, _plan, result = _run(lib_root, z)
    assert result.ok
    assert sorted(c.detected_name for c in kinds(candidates, "symbol")) == [
        "C_0603", "L_0805", "R_0603",
    ]
    lib = lb.scan(lib_root)
    assert len(lib.symbols) == 3 and len(lib.footprints) == 3 and len(lib.models) == 3


def test_macos_resource_forks_are_ignored(tmp_path, lib_root):
    z = kf.zip_with_macos_junk(tmp_path / "mac.zip")
    with ingest.open_bundle(z) as bundle:
        candidates = ingest.detect(bundle)
        assert any("__MACOSX" in i for i in bundle.ignored)
        assert any(".DS_Store" in i for i in bundle.ignored)
    # one symbol and one footprint, not three of each
    assert len(kinds(candidates, "symbol")) == 1
    assert len(kinds(candidates, "footprint")) == 1


def test_zip_slip_members_are_refused(tmp_path, lib_root):
    z = kf.zip_unsafe_paths(tmp_path / "evil.zip")
    with ingest.open_bundle(z) as bundle:
        candidates = ingest.detect(bundle)
        assert any("unsafe archive member" in w for w in bundle.warnings)
    assert [c.detected_name for c in candidates] == ["Good"]
    assert not (tmp_path / "escaped.kicad_sym").exists()


@pytest.mark.parametrize("builder", [
    kf.zip_snapeda, kf.zip_ultra_librarian, kf.zip_component_search_engine,
])
def test_the_common_vendor_layouts_all_work(builder, tmp_path, lib_root):
    z = builder(tmp_path / "vendor.zip")
    candidates, _plan, result = _run(lib_root, z)
    assert result.ok
    assert len(kinds(candidates, "symbol")) == 1
    assert len(kinds(candidates, "footprint")) == 1
    lib = lb.scan(lib_root)
    assert len(lib.symbols) == 1 and len(lib.footprints) == 1 and len(lib.models) == 1
    assert lb.scan(lib_root).resolve().dangling == []


def test_datasheets_and_readmes_are_not_imported(tmp_path, lib_root):
    z = kf.zip_snapeda(tmp_path / "s.zip")
    candidates, _plan, _r = _run(lib_root, z)
    assert all(c.kind in ("symbol", "footprint", "model") for c in candidates)
    assert not any(c.source_file.suffix == ".pdf" for c in candidates)


def test_a_plain_folder_source_works(tmp_path, lib_root):
    src = tmp_path / "part"
    kf.write_symbol(src / "NE555.kicad_sym", footprint="DIP8")
    kf.write_footprint(src / "DIP8.kicad_mod")
    kf.write_step(src / "DIP8.step")
    _c, _p, result = _run(lib_root, src)
    assert result.ok
    assert lb.scan(lib_root).resolve().dangling == []


# --------------------------------------------------------------------------
# The lone .kicad_mod case
# --------------------------------------------------------------------------

def test_a_lone_footprint_file_imports_cleanly(tmp_path, lib_root):
    """
    Reproduces the bug that created this repo's '3255' category: selecting a
    single .kicad_mod made the old code search *inside the file*, copy
    nothing, create three empty directories and write a manifest record with
    files: {}.
    """
    mod = kf.write_footprint(tmp_path / "SOP63P810X120-44N.kicad_mod")
    candidates, _plan, result = _run(lib_root, mod)
    assert result.ok
    assert [c.detected_name for c in candidates] == ["SOP63P810X120-44N"]
    lib = lb.scan(lib_root)
    assert len(lib.footprints) == 1
    assert lib.find_footprint(CAT, "SOP63P810X120-44N") is not None


def test_a_lone_footprint_creates_no_empty_sibling_directories(tmp_path, lib_root):
    """
    Importing only a footprint must not create a symbol or 3D directory.
    Empty directories are what git refuses to track, so the other machine
    ended up with table entries pointing at folders that did not exist.
    """
    mod = kf.write_footprint(tmp_path / "FP_ONLY.kicad_mod")
    _run(lib_root, mod)
    assert (lib_root / "footprints" / f"{CAT}.pretty").is_dir()
    assert not (lib_root / "symbols" / f"{CAT}.kicad_symdir").exists()
    assert not (lib_root / "3dmodels" / f"{CAT}.3dshapes").exists()
    assert lb.scan(lib_root).categories[CAT].is_empty is False


def test_importing_nothing_creates_no_directories_at_all(lib_root, tmp_path):
    empty = tmp_path / "empty_dir"
    empty.mkdir()
    _c, plan, result = _run(lib_root, empty)
    assert result.ok and plan.is_empty
    assert any("nothing selected" in w for w in plan.warnings)
    assert list((lib_root / "symbols").iterdir()) == []


# --------------------------------------------------------------------------
# Multi-symbol splitting
# --------------------------------------------------------------------------

def test_a_multi_symbol_library_is_split_one_file_per_symbol(tmp_path, lib_root):
    """AGENTS.md 6.1: 22784/22784 official files hold exactly one symbol."""
    z = kf.zip_multi_symbol_cache(tmp_path / "cache.zip")
    _c, _p, result = _run(lib_root, z)
    assert result.ok
    lib = lb.scan(lib_root)
    assert len(lib.symbols) == 5
    for sym in lib.symbols:
        text = sx.read_text(sym.path)
        assert len(sx.top_level_symbols(text)) == 1
        assert sym.extra_symbols == ()


def test_each_split_file_is_named_after_its_symbol(tmp_path, lib_root):
    z = kf.zip_multi_symbol_cache(tmp_path / "cache.zip")
    _run(lib_root, z)
    names = sorted(s.name for s in lb.scan(lib_root).symbols)
    assert names == [
        "CONN-5MM-4P", "DC_Converter_V100-EY9", "Inductor_2_10uH_Leaded_7W15",
        "Mini-Fit_MX4.2_HX-5569-2x2A", "TPA3255DDV",
    ]


def test_split_files_keep_the_internal_name_matching_the_filename(tmp_path, lib_root):
    z = kf.zip_multi_symbol_cache(tmp_path / "cache.zip")
    _run(lib_root, z)
    for sym in lb.scan(lib_root).symbols:
        assert sym.name_matches_internal, f"{sym.name} vs {sym.internal_name}"


def test_illegal_characters_in_a_symbol_name_are_sanitized_and_reported(tmp_path, lib_root):
    z = kf.zip_multi_symbol_cache(tmp_path / "cache.zip")
    candidates, _p, _r = _run(lib_root, z)
    star = next(c for c in candidates if "*" in c.detected_name)
    assert star.target_name == "Inductor_2_10uH_Leaded_7W15"
    assert any("illegal characters" in n for n in star.notes)


# --------------------------------------------------------------------------
# Pairing and reference rewriting
# --------------------------------------------------------------------------

def test_symbol_footprint_property_is_rewritten_to_the_new_category(tmp_path, lib_root):
    z = kf.zip_snapeda(tmp_path / "s.zip")
    _run(lib_root, z)
    sym = lb.scan(lib_root).find_symbol(CAT, "TPA3255DDV")
    assert sym.footprint_ref == f"{CAT}:SOP63P810X120-44N"


def test_the_model_is_named_after_the_footprint_not_the_symbol(tmp_path, lib_root):
    """
    Plan problem #3: ingest named the STEP after the symbol while the
    packager globbed for '<footprint>*.step', so the model was never packaged.
    """
    z = kf.zip_snapeda(tmp_path / "s.zip", part="TPA3255DDV", footprint="SOP63P810X120-44N")
    _run(lib_root, z)
    models = lb.scan(lib_root).models
    assert [m.filename for m in models] == ["SOP63P810X120-44N.step"]


def test_the_footprint_model_path_is_canonical_and_resolves(tmp_path, lib_root):
    z = kf.zip_snapeda(tmp_path / "s.zip")
    _run(lib_root, z)
    lib = lb.scan(lib_root)
    fp = lib.find_footprint(CAT, "SOP63P810X120-44N")
    assert fp.model_paths == (
        f"${{KICAD_CUSTOM_LIB}}/3dmodels/{CAT}.3dshapes/SOP63P810X120-44N.step",
    )
    assert lib.resolve().dangling == []


def test_a_model_block_is_created_when_the_footprint_has_none(tmp_path, lib_root):
    """The real SOP63P810X120-44N.kicad_mod ships without a (model ...) block."""
    src = tmp_path / "part"
    kf.write_symbol(src / "P.kicad_sym", footprint="FP")
    kf.write_footprint(src / "FP.kicad_mod")   # no model
    kf.write_step(src / "P.step")
    _run(lib_root, src)
    fp = lb.scan(lib_root).find_footprint(CAT, "FP")
    assert len(fp.model_paths) == 1
    assert lb.scan(lib_root).resolve().dangling == []


def test_a_hand_tuned_offset_survives_the_import(tmp_path, lib_root):
    src = tmp_path / "part"
    kf.write_footprint(src / "FP.kicad_mod", model="old.step",
                       offset=(0.1, 0.2, 0.3), rotate=(0, 0, 90))
    kf.write_step(src / "old.step")
    _run(lib_root, src)
    text = sx.read_text(lb.scan(lib_root).find_footprint(CAT, "FP").path)
    m = sx.all_models(text)[0]
    assert (m.offset, m.rotate) == ((0.1, 0.2, 0.3), (0.0, 0.0, 90.0))


def test_unmatched_symbols_are_warned_about_not_silently_relabelled(tmp_path, lib_root):
    """
    The real staged bundle: 7 of 8 symbols point at a project library
    ('TPA3255_BTL_Mono:...') that is not in the bundle. Their Footprint
    property must be left alone and the gap reported.
    """
    z = kf.zip_multi_symbol_cache(tmp_path / "cache.zip")
    _c, plan, _r = _run(lib_root, z)
    assert sum("could not be matched to a footprint" in w for w in plan.warnings) >= 4
    refs = lb.scan(lib_root).resolve()
    assert any(d.target.startswith("TPA3255_BTL_Mono:") for d in refs.dangling)


def test_an_unpaired_model_is_reported_and_not_imported(tmp_path, lib_root):
    src = tmp_path / "part"
    kf.write_symbol(src / "S.kicad_sym")
    kf.write_step(src / "stray_a.step")
    kf.write_step(src / "stray_b.step")
    _c, plan, _r = _run(lib_root, src)
    assert sum("not referenced by any footprint" in w for w in plan.warnings) == 2
    assert lb.scan(lib_root).models == []


def test_pairing_prefers_the_footprint_property_over_a_matching_stem(tmp_path, lib_root):
    src = tmp_path / "part"
    kf.write_symbol(src / "P.kicad_sym", footprint="Vendor:REAL_FP")
    kf.write_footprint(src / "P.kicad_mod", name="P")          # same stem, a decoy
    kf.write_footprint(src / "REAL_FP.kicad_mod")
    with ingest.open_bundle(src) as b:
        cands = ingest.detect(b)
        pairing = ingest.autopair(cands)
    sym = next(c for c in cands if c.kind == "symbol")
    chosen = pairing.symbol_to_footprint[sym.key]
    assert chosen is not None and "REAL_FP" in chosen


# --------------------------------------------------------------------------
# Conflicts
# --------------------------------------------------------------------------

def test_default_policy_skips_an_existing_item_and_warns(tmp_path, lib_root):
    z = kf.zip_snapeda(tmp_path / "s.zip")
    _run(lib_root, z)
    original = sx.read_text(lb.scan(lib_root).find_symbol(CAT, "TPA3255DDV").path)

    _c, plan, result = _run(lib_root, z)
    assert result.ok
    assert any("already exists" in w for w in plan.warnings)
    assert sx.read_text(lb.scan(lib_root).find_symbol(CAT, "TPA3255DDV").path) == original
    assert len(lb.scan(lib_root).symbols) == 1


def test_overwrite_policy_replaces_the_item(tmp_path, lib_root):
    z = kf.zip_snapeda(tmp_path / "s.zip")
    _run(lib_root, z)
    _c, plan, result = _run(lib_root, z, conflict=ops.ConflictPolicy.OVERWRITE)
    assert result.ok
    assert any("overwriting" in w for w in plan.warnings)
    assert len(lb.scan(lib_root).symbols) == 1


def test_rename_policy_imports_alongside_the_existing_item(tmp_path, lib_root):
    z = kf.zip_snapeda(tmp_path / "s.zip")
    _run(lib_root, z)
    _c, _plan, result = _run(lib_root, z, conflict=ops.ConflictPolicy.RENAME)
    assert result.ok
    names = sorted(s.name for s in lb.scan(lib_root).symbols)
    assert names == ["TPA3255DDV", "TPA3255DDV_1"]


# --------------------------------------------------------------------------
# Selection, provenance, hygiene
# --------------------------------------------------------------------------

def test_select_limits_what_is_imported(tmp_path, lib_root):
    z = kf.zip_multi_part(tmp_path / "parts.zip")
    _c, _p, result = _run(lib_root, z, select=["R_0603", "R_0603_1608Metric"])
    assert result.ok
    assert [s.name for s in lb.scan(lib_root).symbols] == ["R_0603"]


def test_provenance_records_the_original_names(tmp_path, lib_root):
    z = kf.zip_snapeda(tmp_path / "snapeda_tpa3255.zip")
    prov = pv.load(lib_root)
    _run(lib_root, z, prov=prov)
    item = prov.get(CAT, pv.KIND_SYMBOL, "TPA3255DDV")
    assert item is not None
    assert item.source == "snapeda_tpa3255.zip"
    model = prov.get(CAT, pv.KIND_MODEL, "SOP63P810X120-44N.step")
    assert model.original_name == "TPA3255DDV.step"


def test_crlf_sources_are_written_as_lf(tmp_path, lib_root):
    src = tmp_path / "part"
    src.mkdir()
    (src / "P.kicad_sym").write_bytes(
        kf.symbol_lib([kf.symbol_block("P", "FP")]).replace("\n", "\r\n").encode()
    )
    (src / "FP.kicad_mod").write_bytes(kf.footprint_text("FP").replace("\n", "\r\n").encode())
    _run(lib_root, src)
    for sym in lb.scan(lib_root).symbols:
        assert b"\r" not in sym.path.read_bytes()
    for fp in lb.scan(lib_root).footprints:
        assert b"\r" not in fp.path.read_bytes()


def test_planning_alone_writes_nothing(tmp_path, lib_root):
    z = kf.zip_snapeda(tmp_path / "s.zip")
    with ingest.prepare(lib_root, z, CAT) as prev:
        assert not prev.plan.is_empty
    assert list((lib_root / "symbols").iterdir()) == []


def test_the_temp_directory_is_removed_when_the_preview_closes(tmp_path, lib_root):
    z = kf.zip_snapeda(tmp_path / "s.zip")
    with ingest.prepare(lib_root, z, CAT) as prev:
        temp = prev.bundle.temp_dir
        assert temp and temp.exists()
        ops.apply(prev.plan)
    assert not temp.exists()


def test_the_temp_directory_is_removed_even_when_planning_raises(tmp_path, lib_root):
    z = kf.zip_snapeda(tmp_path / "s.zip")
    before = set(Path(tmp_path).parent.glob("kicad_ingest_*"))
    with pytest.raises(Exception):
        ingest.prepare(lib_root, z, "Bad:Category")
    assert set(Path(tmp_path).parent.glob("kicad_ingest_*")) == before


def test_a_missing_source_is_a_clear_error(lib_root):
    with pytest.raises(FileNotFoundError, match="does not exist"):
        ingest.open_bundle(Path("/nonexistent/part.zip"))


def test_an_unparseable_symbol_file_warns_and_does_not_abort_the_batch(tmp_path, lib_root):
    src = tmp_path / "part"
    src.mkdir()
    (src / "broken.kicad_sym").write_text('(kicad_symbol_lib\n\t(symbol "X"\n')
    kf.write_footprint(src / "GOOD.kicad_mod")
    _c, plan, result = _run(lib_root, src)
    assert result.ok
    assert any("cannot read as a symbol" in w for w in plan.warnings)
    assert [f.name for f in lb.scan(lib_root).footprints] == ["GOOD"]


def test_an_invalid_category_name_is_refused(tmp_path, lib_root):
    z = kf.zip_snapeda(tmp_path / "s.zip")
    with pytest.raises(Exception):
        ingest.prepare(lib_root, z, "Bad:Name")


def test_a_category_colliding_with_an_official_library_only_warns(tmp_path, lib_root):
    z = kf.zip_snapeda(tmp_path / "s.zip")
    _c, plan, result = _run(lib_root, z, category="Amplifier_Audio")
    assert result.ok
    assert any("official KiCad library" in w for w in plan.warnings)
