# Work Plan: `kicad_customlib` Overhaul

Audience: an LLM coding agent working inside the repo `mraxxi/kicad_customlib`.
Read this whole file, then `AGENTS.md` and `README.md`, before changing anything.
Work phase by phase. Commit after each phase with a clear message. Do not start a phase until the previous phase's acceptance checks pass.

---

## 0. Context

A personal KiCad 10 library (symbols, footprints, 3D models) synced between two machines with git, plus a stdlib-only Python tool (`scripts/lib_manager.py`, CLI + Tkinter GUI) that ingests vendor downloads (ZIP / folders), keeps three directories mirrored per category, patches cross-references, and generates master library tables.

Layout (unchanged, this is correct for KiCad 10; KiCad's own built-in libs use `.kicad_symdir` folders):

```
symbols/<Category>.kicad_symdir/<Symbol>.kicad_sym
footprints/<Category>.pretty/<Footprint>.kicad_mod
3dmodels/<Category>.3dshapes/<Model>.step
sym-lib-table, fp-lib-table      (generated, tracked in git)
scripts/lib_manager.py, scripts/src/{core,gui}/
```

Library nickname = category name. 3D paths use `${KICAD_CUSTOM_LIB}/3dmodels/<Cat>.3dshapes/<file>`. Symbol `Footprint` property = `<Category>:<FootprintName>`.

### Hard rules (from AGENTS.md, keep them)
1. Never corrupt KiCad S-expressions. No naive replace that can eat parentheses, UUIDs, or float formatting. Use/extend `core/s_expr.py` (paren-matching, quote-aware).
2. No absolute paths in any library file or config. Use `${KICAD_CUSTOM_LIB}` (libraries) and `${KIPRJMOD}` (packaged projects).
3. Three-way directory mirroring per category.
4. Stdlib only for the core and the GUI. Optional extras (e.g. `tkinterdnd2`) must degrade gracefully when absent.
5. Must run on Linux (Arch) and Windows/macOS. No POSIX-only behavior, no hard-coded separators, write files with `\n` line endings.

### Decisions already made (do not re-litigate)
- **The disk is the source of truth.** The relationships are derived by scanning: symbol -> footprint via the symbol's `Footprint` property; footprint -> model via its `(model "...")` path.
- **`manifest.json` is replaced** by a slim `provenance.json` (see section 3.2). No global `last_updated`, no `history` array (git is the history).
- A "part" is not 1 symbol + 1 footprint + 1 model. Many symbols may share a footprint; a footprint may have 0..n models.
- Category renames/moves are allowed but must warn that existing projects' `lib_id`s will break.

---

## 1. Verified problems in the current code (fix these)

| # | Where | Problem | Evidence |
|---|-------|---------|----------|
| 1 | `gui/app.py` `on_ingest_dialog` | `askopenfilename` cannot select folders. Picking a lone `.kicad_mod` makes `ingest_part` search *inside a file*, copy nothing, create three empty category dirs, write a manifest record with `files: {}`. Reproduced; it is exactly the committed manifest entry for `SOP63P810X120-44N` / category `3255`. | reproduced |
| 2 | `core/manifest.py` `ingest_part` / `move_part` | Empty category dirs are created eagerly -> `generate_tables` lists empty libs (stale `3255` in `sym-lib-table`). Git does not track empty dirs, so on the other machine the table points at non-existent folders. | reproduced |
| 3 | `core/packager.py` | Looks for `{footprint_name}*.step`, but ingest names the STEP after the *symbol*. 3D models are not packaged when the names differ. The `*` glob can also match the wrong file. | reproduced |
| 4 | `core/packager.py` | Writes `sym-lib-table`/`fp-lib-table` into `project_libs/`, but KiCad reads project tables from the project root. Paths hard-code `${KIPRJMOD}/project_libs/` so `--out` elsewhere breaks. | code |
| 5 | `gui/app.py` | `Treeview.item()["values"][0]` returns `int` for names like `3255` -> manifest lookup fails. Use `iid=`. | known Tk behavior |
| 6 | `gui/app.py` | Category parsed from listbox text via `split(" (")`; breaks for names containing ` (`. | code |
| 7 | `core/manifest.py` `_load_manifest` | Swallows JSON errors and returns an empty manifest; next `save()` wipes data. | code |
| 8 | `core/manifest.py` `ingest_part` | Takes `[0]` of each file list (multi-part ZIPs lose parts); `rglob` also matches macOS `__MACOSX/._*` junk; silent overwrite; manifest keyed by bare part name (collisions across categories). | code |
| 9 | `core/s_expr.py` | `patch_symbol_footprint` uses `re.sub` with a replacement string (breaks if the value contains `\`); patches only the first `Footprint` property; symbol file is renamed but the internal `(symbol "NAME"` is not. `extract_footprint_model_info` returns only the first `(model`. | code |
| 10 | all writers | `write_text` without `newline="\n"` -> CRLF on Windows -> git churn between machines. | code |
| 11 | `core/manifest.py` `audit_health` | Only checks manifest-listed files; AGENTS.md claims it also finds unindexed files and broken footprint pointers. `lib_manager.py check` always exits 0. | code |
| 12 | `lib_manager.py` `sync-staging` | Ignores ZIPs, never archives processed items (README says it does), re-ingests everything on every run, one failure aborts the batch. | code |
| 13 | `lib_manager.py` | GUI import failure prints only `str(e)`; no traceback, no hint for missing Tk (`pacman -S tk`). | code |
| 14 | naming | Examples in AGENTS.md (`MCU_RaspberryPi`, `Connector_*`) collide with official KiCad library nicknames. | KiCad ships these |

---

## 2. Phase 0 - Safety net (small, do first)

Tasks
- Add `.gitattributes`: `* text=auto eol=lf` plus `*.step binary`, `*.wrl binary`.
- Ensure `.gitignore` covers `__pycache__/`, `*.pyc`, `staging-temp/`, `.venv/`, and local GUI config.
- Add `tests/` with `pytest` (dev-only dependency) and a fixtures builder (`tests/conftest.py`) that creates, in a temp dir: a minimal valid `.kicad_sym`, `.kicad_mod` (with and without a `(model` block), a dummy `.step`, and ZIPs mimicking SnapEDA / Ultra Librarian / Component Search Engine layouts, including a ZIP with `__MACOSX/._X.kicad_sym` junk and a ZIP with multiple parts.
- Do **not** delete anything in `symbols/`, `footprints/`, `3dmodels/` yet. Report empty/stale categories (e.g. `3255`) and ask the owner before removing.

Before implementing symbol handling, **inspect the official `.kicad_symdir` folders** on this machine (`/usr/share/kicad/symbols/Amplifier_Audio.kicad_symdir` and a few others). Confirm and document in `AGENTS.md`: file naming vs internal symbol name, one symbol per file or not, and how `extends` (derived symbols) is stored. Encode the findings as tests. If any behavior is unclear, keep vendor files whole and emit a warning rather than guessing.

Acceptance: `pytest` runs (even if mostly empty), `.gitattributes` in place, findings written into `AGENTS.md`.

---

## 3. Phase 1 - Core rewrite (`scripts/src/core/`)

### 3.1 Module layout
```
core/
  s_expr.py        # extend (see 3.3)
  naming.py        # validation + sanitising of category/part names
  library.py       # disk scanner -> in-memory index (source of truth)
  provenance.py    # slim provenance.json read/write
  ops.py           # Plan / Operation dataclasses, plan(), apply(), atomic writes
  ingest.py        # candidate detection + planning
  refactor.py      # rename / move planning (symbol, footprint, model, category)
  table_gen.py     # generate + staleness check
  check.py         # full audit
  packager.py      # fixed
```
Keep `ManifestManager` importable only if needed as a thin deprecated shim; remove it once GUI/CLI no longer use it.

### 3.2 Data model

`library.py` scans and returns:
```
Library
  categories: {name: Category(has_sym_dir, has_fp_dir, has_model_dir)}
  symbols:    [Symbol(category, name, path, footprint_ref: "Cat:Name"|None)]
  footprints: [Footprint(category, name, path, models: [model_path_str])]
  models:     [Model(category, filename, path)]
  refs:       resolved links + dangling links (footprint_ref not found, model path not found)
```
Rules: scanning is read-only, cheap (no full parse beyond regex-safe property extraction using the paren-matcher), deterministic ordering (sorted, case-insensitive then case-sensitive tiebreak).

`provenance.json` (only things the disk cannot tell us):
```json
{
  "version": 2,
  "items": {
    "TI-TPAxxx_AUDIO-AMP/symbol/TPA3255DDV": {
      "original_name": "TPA3255DDV.kicad_sym",
      "source": "SnapEDA zip",
      "imported": "2026-10-04"
    }
  }
}
```
- Keys are `<Category>/<kind>/<name>`; sorted keys; `indent=2`; trailing newline; atomic write; no timestamps other than per-item import date (date only).
- Renames/moves update keys. Missing provenance is never an error.
- A corrupt `provenance.json` must raise a clear error and leave the file untouched (back up to `provenance.json.bak` before any rewrite). Never silently reset.
- Provide a one-time `migrate-manifest` command that converts an existing `manifest.json` into `provenance.json` and then leaves the old file for the owner to delete.

### 3.3 `s_expr.py` fixes and additions
- All reads/writes: UTF-8, `newline="\n"` on write; tolerate a BOM and CRLF on read.
- Never use `re.sub` with an interpolated replacement string; use function replacements or slice by paren positions.
- New helpers (all paren/quote-aware):
  - `top_level_symbols(text)` -> list of `(name, start, end)`.
  - `get_property(block, name)` / `set_property(block, name, value)` operating on a specific symbol block, not the first match in the file.
  - `rename_symbol(text, old, new)`: renames the top-level `(symbol "old"` and its unit sub-symbols `"old_<unit>_<style>"`, updates `extends "old"` references inside the same file, and updates the `Value` property only if it equals the old name.
  - `rename_footprint(text, old, new)`: renames `(footprint "old"`.
  - `all_models(text)` -> every `(model ...)` block with path/offset/scale/rotate; `set_model_path(text, index, new_path)`; `add_model`.
- Preserve UUIDs, floats, and indentation. Add round-trip tests: parse -> unchanged-edit -> write must be byte-identical.

### 3.4 `ops.py` (plan/apply)
- `Operation` kinds: `CreateDir`, `CopyFile`, `MoveFile`, `WriteText`, `DeleteFile`, `DeleteDirIfEmpty`.
- Every high-level action first builds a `Plan` (ordered operations + human-readable summary + warnings + conflicts) **without touching disk**. `apply(plan)` executes it.
- Atomic writes: write to a temp file in the same directory, then `os.replace`. If an operation fails mid-plan, stop and report exactly what was applied (no automatic rollback required, but never leave a half-written file).
- Conflict policy per item: `skip`, `overwrite`, `rename` (auto-suffix). Default is **skip with a warning**, never silent overwrite.
- Lazy directory creation: only create `<Cat>.kicad_symdir` / `.pretty` / `.3dshapes` when a file is actually placed there.
- After a successful `apply`, always: regenerate tables, update provenance.
- `--dry-run` / "Preview" simply prints/returns the plan.

### 3.5 Naming (`naming.py`)
- Allowed in category and part names: letters, digits, `_`, `-`, `.`, `+`. Reject `:` (library separator), path separators, leading/trailing space or dot, Windows-reserved names (`CON`, `NUL`, ...), and names > 100 chars.
- Case-insensitive duplicate detection (Windows/macOS file systems).
- Warn when a category nickname equals an official KiCad library name. Read official names from `$KICAD10_SYMBOL_DIR` / `$KICAD10_FOOTPRINT_DIR` or `/usr/share/kicad/{symbols,footprints}` when present; otherwise fall back to a bundled short list. Recommend a short personal prefix. Update the AGENTS.md examples (`MCU_RaspberryPi` etc.) accordingly.

### 3.6 Ingest (`ingest.py`)
Input: a ZIP, a folder, or one or more individual files.
1. Extract ZIPs to a temp dir inside `try/finally`. Reject unsafe member paths. Ignore `__MACOSX/`, any `._*`, `.DS_Store`.
2. Detect **all** candidates: every `.kicad_sym`, `.kicad_mod`, and `.step/.stp/.wrl`. Return a `Candidate` list, not `[0]`.
3. Auto-pairing (suggestion only, user can override in the GUI):
   - symbol -> footprint: basename of the symbol's `Footprint` property if present, else same stem, else (if exactly one of each) pair them.
   - footprint -> model: basename of its `(model` path, else same stem, else (if exactly one of each) pair them.
4. Default names: symbol keeps its internal name; footprint keeps its name; **model file is named after the footprint** (a model belongs to a footprint, not a symbol). This fixes problem #3.
5. For each accepted candidate produce operations: copy file, rename internal names if the user changed them (via `s_expr.rename_*`), set `Footprint` property to `<Category>:<FootprintName>`, set the model path to `${KICAD_CUSTOM_LIB}/3dmodels/<Cat>.3dshapes/<file>`, preserving existing offset/scale/rotate.
6. If a vendor `.kicad_sym` has several top-level symbols, follow the behavior documented in Phase 0; if undocumented, keep whole and warn.
7. Report anything not imported (e.g. unpaired model) as warnings, never drop silently.

### 3.7 Refactor (`refactor.py`) - rename / move
Every operation scans the **whole library** for references, because a symbol in category A may reference a footprint in category B.

- **Rename symbol**: file name, internal name (+ unit sub-symbols, `extends` in the same file), `Value` if equal; provenance key.
- **Rename footprint**: file name, internal `(footprint "name"`, every symbol in the library whose `Footprint` is `Cat:old` -> `Cat:new`; provenance key. Optional checkbox: also rename the model file to match and update the model path.
- **Rename model file**: file name + the `(model` path in every footprint that points to it.
- **Move item to another category**: file move; for footprints update referencing symbols' `Footprint` prefix; for models update referencing footprints' model path; warn if other items still reference the old location.
- **Rename category**: rename all three dirs, rewrite every `Footprint` prefix `Old:` -> `New:` and every model path, regenerate tables.
- Always show the plan (files touched, references rewritten) and a warning that existing KiCad projects using the old library nickname will break (suggest "Edit Symbol Library References" in KiCad afterwards).
- Refuse when the target name exists (unless the user explicitly picks a conflict policy).

### 3.8 Tables (`table_gen.py`)
- List only libraries that contain at least one file. Deterministic output (sorted, no timestamps).
- `is_stale(root) -> bool` and `diff_tables(root)` to compare generated content with the tracked files.
- Keep format `(version 7)` and `type "KiCad"` with the `${KICAD_CUSTOM_LIB}` URIs (matches KiCad 10 behavior).
- Escape/validate names (naming.py already forbids quotes).

### 3.9 Check (`check.py`)
Return structured findings with severity (`error` / `warning` / `info`):
- Tables stale vs disk; empty category dirs; categories missing one of the three mirrors where files exist.
- Symbol `Footprint` pointing to a non-existent footprint; footprint `(model` path not using `${KICAD_CUSTOM_LIB}`, or pointing to a missing file; absolute paths anywhere.
- Orphan footprints / models (referenced by nothing) as `info`.
- Duplicate names differing only by case.
- Provenance entries pointing to deleted items.
- If `kicad-cli` is on `PATH`, parse-check files: `kicad-cli fp export svg` / `kicad-cli sym export svg` on each file into a temp dir; report failures as `error`. Skip silently when `kicad-cli` is absent. Do not require it.
- `lib_manager.py check` exits non-zero when any `error` exists (CI-friendly).

### 3.10 Packager (`packager.py`)
- Find used `lib_id`s and footprints as now (keep the regexes but add tests with real-looking KiCad 10 sch/pcb snippets).
- Resolve each footprint's models by **reading the model path from the footprint file**, not by name globbing; copy exactly those files; rewrite paths to `${KIPRJMOD}/<relative-out>/3dmodels/...` where `<relative-out>` is computed from `--out` relative to the project dir (error if `--out` is outside the project).
- Write **merge-safe project tables at the project root** (`<project>/sym-lib-table`, `<project>/fp-lib-table`): if they exist, parse and add missing `lib` entries without touching existing ones; back up the original first. Do not leave tables inside the output folder.
- Return and display unresolved items (symbols or footprints not found in the custom lib, e.g. official libs) separately, so the user knows what was *not* packaged.
- Only package items that exist in the custom library; guard against nickname collisions with official libraries.

Acceptance (Phase 1): pytest green, covering at least: lone `.kicad_mod` ingest (no empty dirs, clear result), macOS-junk ZIP, multi-part ZIP, conflict policies, symbol/footprint/model/category rename with reference rewriting, round-trip byte identity, table staleness + empty-lib skipping, packager with differently-named symbol/footprint/model, packager `--out` elsewhere, corrupt `provenance.json` handling, CRLF input -> LF output.

---

## 4. Phase 2 - CLI (`scripts/lib_manager.py`)

Commands (all mutating commands support `--dry-run` and `--yes`; default prints the plan then asks for confirmation when a TTY is attached):
```
ingest <src> -c CATEGORY [--select all|<names>] [--conflict skip|overwrite|rename]
sync-staging [--archive]            # handles ZIPs and folders in staging-temp/intake/<Category>/; moves processed items to staging-temp/imported_archive/; continues past failures and reports a summary
rename symbol|footprint|model|category <old> <new> [--update-model-file]
move <kind> <name> --to CATEGORY
generate
check [--json]                      # non-zero exit on errors
package <project> [--out DIR]
migrate-manifest
gui
```
Also:
- Show full tracebacks with `--debug`; print a clear hint when `tkinter` is missing (`pacman -S tk` on Arch, `python3-tk` on Debian/Ubuntu).
- Remove the `ManifestManager(ROOT_DIR)` construction that happens before every command.
- Update `README.md`/`AGENTS.md` examples to the new commands.

Acceptance: every command works from a fresh clone on Linux; CLI integration tests via `subprocess`/`main(argv)`.

---

## 5. Phase 3 - GUI (`scripts/src/gui/`)

The GUI is a thin layer over `plan()` / `apply()`. No business logic in widgets. Split into modules: `app.py` (shell), `browser.py` (tree), `import_dialog.py`, `rename_dialog.py`, `widgets.py` (category picker, status bar, plan preview). Stay on `tkinter`/`ttk`; `tkinterdnd2` optional for drag-and-drop.

### 5.1 Main window
- Left/right split: **Treeview** `Category -> Symbol` (and a toggle for Footprints / 3D models). Columns: Name, Footprint ✓/✗, 3D ✓/✗, Source. Use `iid` = stable key (`kind|category|name`), never rely on displayed text.
- Search box filters live. Sortable columns. Multi-select enabled.
- Right-click and button actions: **Rename...**, **Move to category...**, **Open folder**, **Copy name**, **Delete** (confirm + plan preview).
- Selection and scroll position are preserved after any refresh.
- Status bar at the bottom (last action result, plan warnings). Replace success `messagebox`es with status-bar messages; keep message boxes only for errors and confirmations.
- A banner appears when `table_gen.is_stale()` is true, with a **Regenerate** button; tables also regenerate automatically after every applied plan and once at startup.
- Fonts: `TkDefaultFont` / `TkFixedFont`; set `tk scaling` for HiDPI; no hard-coded light-gray backgrounds.
- Keyboard: `Ctrl+I` import, `F2` rename, `Del` delete, `Ctrl+F` focus search, `Enter`/`Esc` in dialogs.

### 5.2 Category picker widget (reused everywhere)
- Read-only `ttk.Combobox` listing existing categories (alphabetical), plus a final entry **"＋ New category..."** that opens a small modal: name field with live validation (naming.py rules, case-insensitive duplicate check, official-name collision warning), OK/Cancel. After creation the new name is selected (the directories are only created when a file is placed).
- Remember the last used category (stored in a local config file outside the repo, e.g. `~/.config/kicad_customlib/gui.json`, never tracked).

### 5.3 Import dialog (replaces the free-text dialog)
1. Buttons: **Add ZIP(s)...**, **Add folder...**, **Add file(s)...**; drag-and-drop if `tkinterdnd2` is available.
2. After adding, run candidate detection and show a table: `[x] include | kind | detected name | New name (editable) | Category (combobox per row) | note`. A toolbar action **"Apply category to selected"**.
3. Show auto-pairing results (symbol -> footprint -> model) with the option to change a pairing from a dropdown.
4. **Preview changes** button (shows the plan: files created, properties rewritten, conflicts) then **Import**. Conflicts resolved per item: Skip / Overwrite / Rename.
5. Runs in a worker thread with a progress bar and log pane; UI stays responsive; errors per item do not abort the batch.
6. Modal (`transient` + `grab_set`), Cancel button, Enter/Esc bindings.

### 5.4 Rename / Move dialogs
- Rename: new-name entry with live validation; checkbox list of what will also be updated (references in symbols, model file, internal name) with counts, and a plan preview. A visible warning: existing KiCad projects using the old name will break.
- Move: category picker (5.2), same preview.
- Category rename available from the category node's context menu.

### 5.5 Other dialogs
- **Audit**: scrollable, copyable table of all findings with severity, filter by severity, double-click to select the item in the tree.
- **Package**: pick project dir, optional output folder, preview of what would be packaged and what was **not** found, then run.
- **Process staging**: preview what is in `staging-temp/intake/`, per-item results, archive on success.

Acceptance: all flows work without typing a category name; numeric-looking names (`3255`) work; a failed item shows an error but the rest of the batch completes; no UI freeze during import of a large ZIP. Add headless tests for the controller/view-model layer; smoke-test widget creation only when a display is available (`pytest.importorskip` / `xvfb` if present).

---

## 6. Phase 4 - Docs and repo hygiene

- Rewrite `README.md` (layout, setup on a new machine, workflows, GUI tour, CLI reference, how provenance works, what to do after pulling on the other machine).
- Rewrite `AGENTS.md` for the new architecture: disk-as-truth, plan/apply, naming rules, reserved names, how to add a new operation, how to run tests, the Phase 0 findings about `.kicad_symdir`.
- Remove references to `manifest.json` and fix the TPA3155 vs TPA3255 example mismatch.
- Add a GitHub Actions workflow that runs `pytest` and `python scripts/lib_manager.py check` (skip `kicad-cli` steps there).
- Add `CHANGELOG.md` with one entry per phase.

---

## 7. Out of scope (do not do)
- Do not modify KiCad's official libraries or the user's KiCad config files.
- Do not delete or reorganize existing library content without explicit owner approval; report only.
- Do not add runtime dependencies beyond the standard library (dev-only: `pytest`).
- Do not touch `.kicad_sch` / `.kicad_pcb` of live projects (only the packager reads them).

## 8. Working agreement for the agent
- Small commits per phase; run `pytest` and `python scripts/lib_manager.py check` before each commit.
- When unsure about KiCad file-format behavior, check the official examples in `/usr/share/kicad/` or ask; never guess and silently proceed.
- After each phase, write a short summary: what changed, what tests were added, and any open question for the owner.
