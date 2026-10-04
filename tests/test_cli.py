"""Phase 2: the lib_manager CLI, driven through main(argv)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import lib_manager
from src.core import library as lb
from src.core import provenance as pv
from src.core import s_expr as sx
from src.core import table_gen as tg
from tests import kicad_fixtures as kf


@pytest.fixture
def cli(monkeypatch, lib_root):
    """
    Point the CLI at a throwaway library root.

    Returns a callable that runs main(argv) and gives back (exit_code, stdout).
    """
    monkeypatch.setattr(lib_manager, "ROOT_DIR", lib_root)

    def run(*argv, stdin_is_tty=False):
        monkeypatch.setattr(lib_manager.sys.stdin, "isatty", lambda: stdin_is_tty,
                            raising=False)
        code = lib_manager.main(list(argv))
        return code

    run.root = lib_root
    return run


@pytest.fixture
def stocked(cli, tmp_path):
    """A library with one real part in it."""
    z = kf.zip_snapeda(tmp_path / "tpa3255.zip")
    assert cli("ingest", str(z), "-c", "Amp_Test", "--yes") == 0
    return cli


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------

def test_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        lib_manager.main(["--help"])
    assert exc.value.code == 0


def test_every_subcommand_is_wired_up():
    parser = lib_manager.build_parser()
    expected = {"list", "check", "generate", "ingest", "sync-staging", "rename",
                "move", "package", "migrate-manifest", "gui"}
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    found = set()
    for a in actions:
        if isinstance(a.choices, dict):
            found |= set(a.choices)
    assert expected <= found


def test_no_manifest_is_loaded_before_dispatch(cli, monkeypatch):
    """
    The old CLI built a ManifestManager before every command, so a corrupt
    manifest broke even `generate`.
    """
    (cli.root / "manifest.json").write_text("{ totally broken")
    assert cli("generate", "--yes") == 0


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------

def test_list_on_an_empty_library(cli, capsys):
    assert cli("list") == 0
    assert "The library is empty." in capsys.readouterr().out


def test_list_marks_empty_categories(cli, capsys):
    (cli.root / "symbols" / "3255.kicad_symdir").mkdir()
    assert cli("list") == 0
    out = capsys.readouterr().out
    assert "3255  (empty)" in out


def test_list_verbose_shows_the_reference_chain(stocked, capsys):
    assert stocked("list", "-v") == 0
    out = capsys.readouterr().out
    assert "symbol     TPA3255DDV -> Amp_Test:SOP63P810X120-44N" in out
    assert "footprint  SOP63P810X120-44N -> SOP63P810X120-44N.step" in out


def test_list_flags_a_broken_reference(cli, capsys):
    kf.write_symbol(
        cli.root / "symbols" / "C.kicad_symdir" / "S.kicad_sym", footprint="C:GONE"
    )
    assert cli("list", "-v") == 0
    assert "[BROKEN]" in capsys.readouterr().out


def test_list_warns_about_stale_tables(cli, capsys):
    kf.write_symbol(cli.root / "symbols" / "C.kicad_symdir" / "S.kicad_sym")
    assert cli("list") == 0
    assert "master tables are out of date" in capsys.readouterr().out


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------

def test_check_exits_nonzero_on_an_error(cli):
    kf.write_symbol(
        cli.root / "symbols" / "C.kicad_symdir" / "S.kicad_sym", footprint="C:GONE"
    )
    assert cli("check", "--no-kicad-cli") == 1


def test_check_exits_zero_on_a_clean_library(cli):
    tg.generate_tables(cli.root)
    assert cli("check", "--no-kicad-cli") == 0


def test_check_json_is_parseable(cli, capsys):
    tg.generate_tables(cli.root)
    assert cli("check", "--json", "--no-kicad-cli") == 0
    payload = json.loads(capsys.readouterr().out)
    assert "findings" in payload and "counts" in payload


# --------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------

def test_generate_writes_the_tables(cli):
    kf.write_symbol(cli.root / "symbols" / "C.kicad_symdir" / "S.kicad_sym")
    assert cli("generate", "--yes") == 0
    assert (cli.root / "sym-lib-table").exists()
    assert not tg.is_stale(cli.root)


def test_generate_omits_empty_categories(cli, capsys):
    (cli.root / "symbols" / "3255.kicad_symdir").mkdir()
    assert cli("generate", "--yes") == 0
    assert '"3255"' not in (cli.root / "sym-lib-table").read_text()
    assert "omitted from both tables" in capsys.readouterr().out


def test_generate_is_idempotent(cli, capsys):
    kf.write_symbol(cli.root / "symbols" / "C.kicad_symdir" / "S.kicad_sym")
    cli("generate", "--yes")
    capsys.readouterr()
    assert cli("generate", "--yes") == 0
    assert "already up to date" in capsys.readouterr().out


def test_dry_run_changes_nothing(cli, capsys):
    kf.write_symbol(cli.root / "symbols" / "C.kicad_symdir" / "S.kicad_sym")
    assert cli("generate", "--dry-run") == 0
    assert "dry run" in capsys.readouterr().out
    assert not (cli.root / "sym-lib-table").exists()


def test_applying_without_yes_on_a_non_tty_is_refused(cli, capsys):
    """A script or CI job must never mutate the library by accident."""
    kf.write_symbol(cli.root / "symbols" / "C.kicad_symdir" / "S.kicad_sym")
    assert cli("generate") == lib_manager.EXIT_ABORTED
    assert "stdin is not a terminal" in capsys.readouterr().err
    assert not (cli.root / "sym-lib-table").exists()


def test_a_tty_declining_the_prompt_aborts(cli, monkeypatch, capsys):
    kf.write_symbol(cli.root / "symbols" / "C.kicad_symdir" / "S.kicad_sym")
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert cli("generate", stdin_is_tty=True) == lib_manager.EXIT_ABORTED
    assert "Aborted." in capsys.readouterr().out
    assert not (cli.root / "sym-lib-table").exists()


def test_a_tty_accepting_the_prompt_applies(cli, monkeypatch):
    kf.write_symbol(cli.root / "symbols" / "C.kicad_symdir" / "S.kicad_sym")
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert cli("generate", stdin_is_tty=True) == 0
    assert (cli.root / "sym-lib-table").exists()


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------

def test_ingest_imports_and_regenerates_the_tables(cli, tmp_path):
    z = kf.zip_snapeda(tmp_path / "s.zip")
    assert cli("ingest", str(z), "-c", "Amp_Test", "--yes") == 0
    lib = lb.scan(cli.root)
    assert len(lib.symbols) == 1
    assert not tg.is_stale(cli.root)             # regenerated automatically
    assert lib.resolve().dangling == []


def test_ingest_lists_candidates_before_the_plan(cli, tmp_path, capsys):
    z = kf.zip_multi_part(tmp_path / "parts.zip")
    assert cli("ingest", str(z), "-c", "Passives", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Detected 9 candidate(s)" in out
    assert "R_0603" in out and "C_0603" in out and "L_0805" in out


def test_ingest_writes_provenance(cli, tmp_path):
    z = kf.zip_snapeda(tmp_path / "vendor_bundle.zip")
    assert cli("ingest", str(z), "-c", "Amp_Test", "--yes") == 0
    payload = json.loads((cli.root / "provenance.json").read_text())
    assert payload["version"] == 2
    assert "Amp_Test/symbol/TPA3255DDV" in payload["items"]
    assert payload["items"]["Amp_Test/symbol/TPA3255DDV"]["source"] == "vendor_bundle.zip"


def test_ingest_select_limits_what_is_imported(cli, tmp_path):
    z = kf.zip_multi_part(tmp_path / "parts.zip")
    assert cli("ingest", str(z), "-c", "Passives", "--select", "R_0603",
               "R_0603_1608Metric", "--yes") == 0
    assert [s.name for s in lb.scan(cli.root).symbols] == ["R_0603"]


def test_ingest_conflict_rename(cli, tmp_path):
    z = kf.zip_snapeda(tmp_path / "s.zip")
    cli("ingest", str(z), "-c", "Amp_Test", "--yes")
    assert cli("ingest", str(z), "-c", "Amp_Test", "--conflict", "rename", "--yes") == 0
    assert sorted(s.name for s in lb.scan(cli.root).symbols) == [
        "TPA3255DDV", "TPA3255DDV_1",
    ]


def test_ingest_rejects_an_invalid_category(cli, tmp_path, capsys):
    z = kf.zip_snapeda(tmp_path / "s.zip")
    assert cli("ingest", str(z), "-c", "Bad:Name", "--yes") == 1
    assert "lib_id" in capsys.readouterr().err


def test_ingest_reports_a_missing_source(cli, capsys):
    assert cli("ingest", "/nonexistent/part.zip", "-c", "C", "--yes") == 1
    assert "does not exist" in capsys.readouterr().err


def test_ingest_of_a_lone_footprint_creates_no_empty_dirs(cli, tmp_path):
    mod = kf.write_footprint(tmp_path / "FP_ONLY.kicad_mod")
    assert cli("ingest", str(mod), "-c", "Pkg", "--yes") == 0
    assert (cli.root / "footprints" / "Pkg.pretty").is_dir()
    assert not (cli.root / "symbols" / "Pkg.kicad_symdir").exists()
    assert not (cli.root / "3dmodels" / "Pkg.3dshapes").exists()


# --------------------------------------------------------------------------
# rename / move
# --------------------------------------------------------------------------

def test_rename_symbol_with_a_lib_id(stocked):
    assert stocked("rename", "symbol", "Amp_Test:TPA3255DDV", "TPA3255DDV_V2", "--yes") == 0
    lib = lb.scan(stocked.root)
    assert lib.find_symbol("Amp_Test", "TPA3255DDV_V2") is not None
    assert lib.find_symbol("Amp_Test", "TPA3255DDV") is None


def test_rename_symbol_with_a_bare_name_and_category_flag(stocked):
    assert stocked("rename", "symbol", "TPA3255DDV", "NEW", "-c", "Amp_Test", "--yes") == 0
    assert lb.scan(stocked.root).find_symbol("Amp_Test", "NEW") is not None


def test_rename_without_a_category_explains_how_to_give_one(stocked, capsys):
    assert stocked("rename", "symbol", "TPA3255DDV", "NEW", "--yes") == 1
    assert "pass --category" in capsys.readouterr().err


def test_conflicting_categories_are_rejected(stocked, capsys):
    assert stocked("rename", "symbol", "Amp_Test:TPA3255DDV", "NEW",
                   "-c", "Other", "--yes") == 1
    assert "conflicting categories" in capsys.readouterr().err


def test_rename_footprint_updates_the_symbol(stocked):
    assert stocked("rename", "footprint", "Amp_Test:SOP63P810X120-44N",
                   "SOP65P810X120-44N", "--yes") == 0
    lib = lb.scan(stocked.root)
    assert lib.find_symbol("Amp_Test", "TPA3255DDV").footprint_ref == \
        "Amp_Test:SOP65P810X120-44N"
    assert lib.resolve().dangling == []


def test_rename_footprint_with_update_model_file(stocked):
    assert stocked("rename", "footprint", "Amp_Test:SOP63P810X120-44N", "NEW_FP",
                   "--update-model-file", "--yes") == 0
    lib = lb.scan(stocked.root)
    assert [m.filename for m in lib.models] == ["NEW_FP.step"]
    assert lib.resolve().dangling == []


def test_rename_category(stocked):
    assert stocked("rename", "category", "Amp_Test", "TI_Amps", "--yes") == 0
    lib = lb.scan(stocked.root)
    assert "Amp_Test" not in lib.categories
    assert lib.find_symbol("TI_Amps", "TPA3255DDV").footprint_ref == \
        "TI_Amps:SOP63P810X120-44N"
    assert lib.resolve().dangling == []
    assert not tg.is_stale(stocked.root)


def test_rename_warns_that_projects_break(stocked, capsys):
    stocked("rename", "symbol", "Amp_Test:TPA3255DDV", "NEW", "--dry-run")
    assert "will break" in capsys.readouterr().out


def test_move_a_footprint(stocked):
    assert stocked("move", "footprint", "Amp_Test:SOP63P810X120-44N",
                   "--to", "Pkg_SOP", "--yes") == 0
    lib = lb.scan(stocked.root)
    assert lib.find_footprint("Pkg_SOP", "SOP63P810X120-44N") is not None
    assert lib.find_symbol("Amp_Test", "TPA3255DDV").footprint_ref == \
        "Pkg_SOP:SOP63P810X120-44N"


def test_move_reports_an_unknown_item(stocked, capsys):
    assert stocked("move", "symbol", "Amp_Test:NOPE", "--to", "Other", "--yes") == 1
    assert "no symbol" in capsys.readouterr().err


def test_rename_updates_provenance_keys(stocked):
    stocked("rename", "symbol", "Amp_Test:TPA3255DDV", "RENAMED", "--yes")
    payload = json.loads((stocked.root / "provenance.json").read_text())
    assert "Amp_Test/symbol/RENAMED" in payload["items"]
    assert "Amp_Test/symbol/TPA3255DDV" not in payload["items"]


# --------------------------------------------------------------------------
# sync-staging
# --------------------------------------------------------------------------

def test_sync_staging_without_an_intake_directory_is_not_an_error(cli, capsys):
    assert cli("sync-staging", "--yes") == 0
    assert "No intake directory" in capsys.readouterr().out


def test_sync_staging_imports_every_category_folder(cli, tmp_path):
    intake = cli.root / "staging-temp" / "intake"
    kf.zip_snapeda((intake / "Amp_Test" / "tpa.zip"))
    part = intake / "Conn_XT" / "xt60"
    kf.write_symbol(part / "XT60.kicad_sym", footprint="XT60_FP")
    kf.write_footprint(part / "XT60_FP.kicad_mod")

    assert cli("sync-staging", "--yes") == 0
    lib = lb.scan(cli.root)
    assert sorted(lib.categories) == ["Amp_Test", "Conn_XT"]
    assert len(lib.symbols) == 2
    assert not tg.is_stale(cli.root)


def test_sync_staging_continues_past_a_failure(cli, capsys):
    """One unusable item must not abort the batch."""
    intake = cli.root / "staging-temp" / "intake"
    bad = intake / "Broken" / "bad.zip"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"this is not a zip file")
    kf.zip_snapeda(intake / "Amp_Test" / "good.zip")

    code = cli("sync-staging", "--yes")
    out = capsys.readouterr().out
    assert lb.scan(cli.root).find_symbol("Amp_Test", "TPA3255DDV") is not None
    assert "Imported 1 item(s); 1 failed." in out
    assert "nothing importable found" in out
    assert code == lib_manager.EXIT_ERROR     # reported, but the rest went through


def test_sync_staging_archive_moves_processed_items(cli):
    intake = cli.root / "staging-temp" / "intake"
    z = kf.zip_snapeda(intake / "Amp_Test" / "tpa.zip")
    assert cli("sync-staging", "--archive", "--yes") == 0
    assert not z.exists()
    assert (cli.root / "staging-temp" / "imported_archive" / "tpa.zip").exists()


def test_sync_staging_without_archive_leaves_the_source(cli):
    intake = cli.root / "staging-temp" / "intake"
    z = kf.zip_snapeda(intake / "Amp_Test" / "tpa.zip")
    assert cli("sync-staging", "--yes") == 0
    assert z.exists()


def test_sync_staging_dry_run_imports_nothing(cli):
    intake = cli.root / "staging-temp" / "intake"
    kf.zip_snapeda(intake / "Amp_Test" / "tpa.zip")
    assert cli("sync-staging", "--dry-run") == 0
    assert lb.scan(cli.root).symbols == []


# --------------------------------------------------------------------------
# package
# --------------------------------------------------------------------------

def test_package_exports_and_writes_project_tables(stocked, tmp_path):
    project = kf.write_project(
        tmp_path / "board",
        symbols=[("Amp_Test:TPA3255DDV", "Amp_Test:SOP63P810X120-44N")],
        pcb_footprints=["Amp_Test:SOP63P810X120-44N"],
    )
    assert stocked("package", str(project), "--yes") == 0
    assert (project / "sym-lib-table").exists()
    assert (project / "project_libs" / "symbols" / "Amp_Test.kicad_symdir"
            / "TPA3255DDV.kicad_sym").exists()
    packaged_fp = (project / "project_libs" / "footprints" / "Amp_Test.pretty"
                   / "SOP63P810X120-44N.kicad_mod")
    assert sx.all_models(sx.read_text(packaged_fp))[0].path.startswith("${KIPRJMOD}/")


def test_package_reports_unresolved_items(stocked, tmp_path, capsys):
    project = kf.write_project(
        tmp_path / "board", symbols=[("Device:R", "Resistor_SMD:R_0603_1608Metric")]
    )
    assert stocked("package", str(project), "--yes") == 0
    assert "Not in the custom library" in capsys.readouterr().out


def test_package_refuses_an_out_dir_outside_the_project(stocked, tmp_path, capsys):
    project = kf.write_project(tmp_path / "board", symbols=[])
    assert stocked("package", str(project), "--out", str(tmp_path / "elsewhere"),
                   "--yes") == 1
    assert "must be inside the project" in capsys.readouterr().err


def test_package_dry_run_writes_nothing(stocked, tmp_path):
    project = kf.write_project(
        tmp_path / "board", symbols=[("Amp_Test:TPA3255DDV", "Amp_Test:SOP63P810X120-44N")]
    )
    assert stocked("package", str(project), "--dry-run") == 0
    assert not (project / "project_libs").exists()
    assert not (project / "sym-lib-table").exists()


# --------------------------------------------------------------------------
# migrate-manifest
# --------------------------------------------------------------------------

def test_migrate_manifest_drops_records_with_nothing_on_disk(cli, capsys):
    (cli.root / "manifest.json").write_text(json.dumps({
        "version": 1,
        "parts": {
            "SOP63P810X120-44N": {
                "category": "3255", "files": {},
                "import_date": "2026-10-04T15:07:47+00:00",
            }
        },
    }))
    assert cli("migrate-manifest", "--yes") == 0
    out = capsys.readouterr().out
    assert "dropped 3255/SOP63P810X120-44N" in out
    assert not (cli.root / "provenance.json").exists()   # nothing worth writing


def test_migrate_manifest_keeps_the_old_file(cli):
    kf.write_symbol(cli.root / "symbols" / "Amp.kicad_symdir" / "S.kicad_sym")
    (cli.root / "manifest.json").write_text(json.dumps({
        "version": 1,
        "parts": {"S": {
            "category": "Amp", "source_meta": "SnapEDA",
            "files": {"symbol": "symbols/Amp.kicad_symdir/S.kicad_sym"},
        }},
    }))
    assert cli("migrate-manifest", "--yes") == 0
    assert (cli.root / "manifest.json").exists()
    payload = json.loads((cli.root / "provenance.json").read_text())
    assert payload["items"]["Amp/symbol/S"]["source"] == "SnapEDA"


def test_migrate_manifest_dry_run_shows_the_result(cli, capsys):
    kf.write_symbol(cli.root / "symbols" / "Amp.kicad_symdir" / "S.kicad_sym")
    (cli.root / "manifest.json").write_text(json.dumps({
        "version": 1,
        "parts": {"S": {"category": "Amp",
                        "files": {"symbol": "symbols/Amp.kicad_symdir/S.kicad_sym"}}},
    }))
    assert cli("migrate-manifest", "--dry-run") == 0
    assert "would contain" in capsys.readouterr().out
    assert not (cli.root / "provenance.json").exists()


# --------------------------------------------------------------------------
# prune-provenance
# --------------------------------------------------------------------------

def test_prune_provenance_without_a_file_is_not_an_error(cli, capsys):
    assert cli("prune-provenance", "--yes") == 0
    assert "No provenance.json" in capsys.readouterr().out


def test_prune_provenance_on_a_clean_library_does_nothing(stocked, capsys):
    assert stocked("prune-provenance", "--yes") == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_prune_provenance_rehomes_a_stale_category(stocked, capsys):
    """The repair for what the plan-time mutation bug left behind."""
    prov = pv.load(stocked.root)
    prov.items["Amp_T/symbol/TPA3255DDV"] = prov.items.pop("Amp_Test/symbol/TPA3255DDV")
    prov.save()

    assert stocked("prune-provenance", "--yes") == 0
    out = capsys.readouterr().out
    assert "Re-home 1" in out
    after = pv.load(stocked.root)
    assert after.get("Amp_Test", pv.KIND_SYMBOL, "TPA3255DDV") is not None
    assert "Amp_T/symbol/TPA3255DDV" not in after.items


def test_prune_provenance_drops_what_it_cannot_place_and_says_what_it_was(stocked, capsys):
    prov = pv.load(stocked.root)
    prov.record("Amp_Test", pv.KIND_SYMBOL, "VANISHED", original_name="ghost.kicad_sym")
    prov.save()

    assert stocked("prune-provenance", "--yes") == 0
    out = capsys.readouterr().out
    assert "Drop 1" in out
    assert "ghost.kicad_sym" in out          # printed before it is lost
    assert "Amp_Test/symbol/VANISHED" not in pv.load(stocked.root).items


def test_prune_provenance_dry_run_changes_nothing(stocked, capsys):
    prov = pv.load(stocked.root)
    prov.record("Amp_Test", pv.KIND_SYMBOL, "VANISHED")
    prov.save()
    before = (stocked.root / "provenance.json").read_bytes()

    assert stocked("prune-provenance", "--dry-run") == 0
    assert "dry run" in capsys.readouterr().out
    assert (stocked.root / "provenance.json").read_bytes() == before


def test_prune_provenance_drop_only_skips_recovery(stocked, capsys):
    prov = pv.load(stocked.root)
    prov.items["Amp_T/symbol/TPA3255DDV"] = prov.items.pop("Amp_Test/symbol/TPA3255DDV")
    prov.save()

    assert stocked("prune-provenance", "--drop-only", "--yes") == 0
    out = capsys.readouterr().out
    assert "Re-home" not in out
    assert "Drop 1" in out
    assert pv.load(stocked.root).get("Amp_Test", pv.KIND_SYMBOL, "TPA3255DDV") is None


def test_prune_provenance_reports_a_corrupt_file(stocked, capsys):
    (stocked.root / "provenance.json").write_text("{broken")
    assert stocked("prune-provenance", "--yes") == 1
    assert "not valid JSON" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Error reporting
# --------------------------------------------------------------------------

def test_an_unexpected_error_suggests_debug(cli, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(lib_manager.tg, "plan_generate", boom)
    assert cli("generate", "--yes") == 1
    err = capsys.readouterr().err
    assert "synthetic failure" in err
    assert "--debug" in err


def test_debug_prints_a_traceback(cli, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(lib_manager.tg, "plan_generate", boom)
    assert cli("--debug", "generate", "--yes") == 1
    assert "Traceback" in capsys.readouterr().err


def test_a_missing_tkinter_gives_an_install_hint(cli, monkeypatch, capsys):
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def fake_import(name, *a, **k):
        if name.startswith("src.gui") or name == "tkinter":
            raise ImportError("No module named 'tkinter'")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert cli("gui") == 1
    err = capsys.readouterr().err
    assert "needs Tk" in err
    assert "pacman -S tk" in err
    assert "available from the CLI" in err
