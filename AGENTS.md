# AI Agent Operating Guidelines — KiCad Custom Library (`KICAD_CUSTOM_LIB`)

> **Scope**: how AI coding assistants (Claude, Gemini, Antigravity, MCP Konnect,
> ChatGPT, …) must interact with, modify and extend this repository.

A personal KiCad 10 library — symbols, footprints and 3D models — synced
between two machines with git, plus a stdlib-only Python tool
(`scripts/lib_manager.py`, CLI + Tkinter GUI) that ingests vendor downloads,
keeps the three directory trees mirrored, patches cross-references, and
generates the master library tables.

---

## 1. Architecture: two rules that explain everything else

### Rule A — The disk is the source of truth
There is no database of parts. `core/library.py` scans the three directory
trees and *derives* every relationship:

* symbol → footprint, from the symbol's `Footprint` property;
* footprint → 3D model, from the footprint's own `(model "…")` path.

A "part" is therefore **not** one symbol + one footprint + one model. Several
symbols may share a footprint, a footprint may carry zero or many models, and
a reference may legitimately cross categories.

`provenance.json` holds only what scanning cannot recover — the original
vendor filename, where it came from, and the import date. It is advisory:
**missing provenance is never an error.** (The old `manifest.json` was the
source of truth, and a stale record made the library appear to contain things
it did not. It is gone; `lib_manager.py migrate-manifest` converts an old one.)

### Rule B — Plan, then apply
Nothing writes to disk directly. Every mutating action builds an `ops.Plan`
— a pure computation that touches nothing — which the CLI prints and the GUI
displays, and only then does `ops.apply()` run it.

```python
plan = ingest.plan_ingest(root, category, candidates)   # reads only
print(plan.summary())                                   # exact preview
result = ops.apply(plan)                                # now it writes
```

**When adding a new operation, add a `plan_*` function. Never write to the
library from inside a UI callback or a scanning function.**

Consequences worth internalising:
* Conflicts default to **skip with a warning**. Overwriting is opt-in.
* Directories are created **lazily**, by the file operations themselves.
  Never pre-create the three mirror directories: git does not track an empty
  directory, so an empty category exists on one machine and not the other.
  (This is exactly how the stale `3255` category came about.)
* Writes are atomic — temp file in the destination directory, then
  `os.replace` — so no KiCad file is ever left half-written.
* After a successful apply, always regenerate the tables and save provenance.

---

## 2. Golden rules of library integrity

### Rule 1: Never corrupt KiCad S-expressions
KiCad files are serialized S-expression trees with order dependence, nested
parentheses and UUIDs. **Always** go through `core/s_expr.py` (or Konnect MCP
tools for live project files).

* **Never** `re.sub` with an interpolated replacement string. A value
  containing a backslash — a Windows path, an escaped quote — is reinterpreted
  as a group reference and corrupts the file. `s_expr` computes
  `(start, end, replacement)` edit spans and applies them back-to-front.
* **Never** regex across a whole file for something that belongs to one block.
  `get_property`/`set_property` take a block offset precisely because the old
  code patched the first `Footprint` property in the file and mislabelled every
  other symbol in a multi-symbol bundle.
* Reads tolerate a BOM and CRLF; writes always emit LF. Read-then-write is
  byte-identical, and there are round-trip tests to keep it that way.

### Rule 2: Absolute path ban
**Never** hardcode a local path into any library file or config.

3D model paths inside `.kicad_mod`:
```scheme
(model "${KICAD_CUSTOM_LIB}/3dmodels/<Category>.3dshapes/<ModelName>.step"
    (offset (xyz <X> <Y> <Z>))
    (scale  (xyz <Sx> <Sy> <Sz>))
    (rotate (xyz <Rx> <Ry> <Rz>))
)
```
Symbol footprint fields inside `.kicad_sym`:
```scheme
(property "Footprint" "<Category>:<FootprintName>" …)
```
Packaged project bundles use `${KIPRJMOD}` instead. `check` reports any path
that is absolute, relative or uses another variable.

When repointing a model, replace **only the path token** — a hand-tuned
offset/scale/rotate in the footprint is the owner's alignment work and must
survive a rename or a category move.

### Rule 3: Directory mirroring
A category's three directories share one base name:

1. `symbols/<Category>.kicad_symdir/`
2. `footprints/<Category>.pretty/`
3. `3dmodels/<Category>.3dshapes/`

The *names* must match exactly. A directory should exist only when it holds a
file — see Rule B. A symbol-only or footprint-only category is normal and
`check` reports it as a note, not an error.

### Rule 4: One symbol per `.kicad_sym`
All 22 784 official KiCad 10 symbol files hold exactly one top-level
`(symbol …)`. A multi-symbol vendor bundle or project cache library must be
**split**, one file per symbol, named after the symbol. See §7.1.

### Rule 5: Stdlib only
Core and GUI use the standard library only. `tkinterdnd2` is optional and must
degrade gracefully. `pytest` is a dev-only dependency. Everything must run on
Linux, Windows and macOS: no POSIX-only behaviour, no hard-coded separators,
`\n` line endings on write.

---

## 3. Directory layout & roles

| Path | Description | Git |
|---|---|---|
| `sym-lib-table`, `fp-lib-table` | Master tables, generated (v7 format). Only non-empty libraries are listed. | Yes |
| `provenance.json` | Original vendor names, sources, import dates. Advisory. | Yes |
| `symbols/` | `*.kicad_symdir/` directories of `*.kicad_sym` | Yes |
| `footprints/` | `*.pretty/` directories of `*.kicad_mod` | Yes |
| `3dmodels/` | `*.3dshapes/` directories of `.step`/`.stp`/`.wrl` | Yes |
| `template/` | Project templates, page layouts (`*.kicad_wks`) | Yes |
| `staging-temp/` | Local intake scratchpad. `intake/<Category>/` in, `imported_archive/` out. | **No** |
| `scripts/` | CLI, core engine and GUI | Yes |
| `tests/` | pytest suite | Yes |
| `manifest.json` | **Obsolete.** Superseded by `provenance.json`. | legacy |

### Module map (`scripts/src/`)
```
core/
  naming.py      name validation, sanitising, official-nickname collisions
  s_expr.py      quote/paren-aware KiCad S-expression read, edit, write
  library.py     read-only scanner -> index + reference resolution
  provenance.py  provenance.json, and migration from manifest.json
  ops.py         Plan / Operation / apply, conflict policies, atomic writes
  ingest.py      candidate detection, auto-pairing, import planning
  refactor.py    rename & move with library-wide reference rewriting
  table_gen.py   table generation and staleness
  check.py       structured audit with severities
  packager.py    lean per-project export
  vcs.py         git state and the four safe operations; no force push, no reset
gui/
  controller.py  view-model: every decision, no Tk -> tested headlessly
  widgets.py     category picker, plan preview, status bar, sortable tree
  browser.py     category list + item table
  sync_dialog.py git state and the four safe actions, on a worker thread
  import_dialog.py, rename_dialog.py, app.py
```

Two GUI conventions, both enforced by tests:

* **Paddings come from the 8px grid in `widgets.py`** (`PAD_DIALOG`,
  `PAD_SECTION`, `PAD_BAR`, `GAP`, `GAP_XS`, `GAP_M`). A non-zero numeric
  `padding=`/`pady=`/`padx=` anywhere in `gui/` fails
  `test_no_module_hardcodes_a_padding`.
* **Never block the Tk event loop.** `update()` dispatches input events and
  re-enters the callback you are inside; `update_idletasks()` does not
  process incoming expose or configure events, so the window stops resizing
  and repaints as a black rectangle. Anything that waits -- a subprocess, a
  network call -- is polled with `after()` or run on a thread that reports
  back through a queue. `gui/filepicker.py` carries the measurements.
* **Press the button in the test.** Constructing a dialog and inspecting its
  contents does not execute a single command callback, and Tk swallows an
  exception raised in one (`report_callback_exception` prints and returns, so
  `invoke()` completes normally). `tests/test_gui_buttons.py` presses
  everything with a root that re-raises; two missing imports reached a user
  because nothing did this.
* **Never ask Tk whether your own widget is showing.** `winfo_ismapped()`
  returns 0 for a correctly packed widget in a window that has not been
  mapped yet, and construction-time code runs before mapping. Track the state
  in an attribute instead -- the sash restore, the layout capture and the
  empty-state swap were each broken by this.

---

## 4. Standard procedures

Every mutating command prints its plan first. `--dry-run` stops there; `--yes`
skips the confirmation. A non-interactive run without `--yes` is **refused**,
so a script can never mutate the library by accident.

```bash
# What is actually in here?
python scripts/lib_manager.py list -v

# Audit. Exits non-zero on errors, so it can gate CI.
python scripts/lib_manager.py check            # --json for machine output

# Import a vendor download (ZIP, folder, or a single file)
python scripts/lib_manager.py ingest ~/Downloads/part.zip -c <Category> --dry-run
python scripts/lib_manager.py ingest ~/Downloads/part.zip -c <Category> --yes
#   --select NAME ...                import only some items
#   --conflict skip|overwrite|rename  default: skip with a warning

# Batch-import staging-temp/intake/<Category>/
python scripts/lib_manager.py sync-staging --archive

# Rename / move. References across the whole library are rewritten.
python scripts/lib_manager.py rename symbol    <Cat>:<Old> <New>
python scripts/lib_manager.py rename footprint <Cat>:<Old> <New> --update-model-file
python scripts/lib_manager.py rename model     <Cat>:<old.step> <new.step>
python scripts/lib_manager.py rename category  <Old> <New>
python scripts/lib_manager.py move   symbol    <Cat>:<Name> --to <NewCat>

# Regenerate the master tables
python scripts/lib_manager.py generate

# Export only what a project uses, as a ${KIPRJMOD} bundle
python scripts/lib_manager.py package ~/projects/amp

# One-time: manifest.json -> provenance.json
python scripts/lib_manager.py migrate-manifest

# Git, for the two-machine setup. Only safe operations: there is no force
# push, reset, stash, rebase or checkout anywhere in core/vcs.py.
python scripts/lib_manager.py sync status      # --json for machine output
python scripts/lib_manager.py sync fetch
python scripts/lib_manager.py sync pull        # --ff-only, then re-audits
python scripts/lib_manager.py sync commit      # message generated from the diff
python scripts/lib_manager.py sync push        # audits first; --skip-check overrides

python scripts/lib_manager.py gui              # or no arguments
```

**Never move or rename library files by hand.** The scripts rewrite the
`Footprint` property of every referencing symbol, the `(model …)` path of
every referencing footprint, and `(extends …)` in sibling symbol files. Doing
it manually leaves silent dangling references that only surface when KiCad
fails to load a part.

### Adding a new operation
1. Add a `plan_*` function to the right `core/` module. It reads the disk and
   returns an `ops.Plan`; it must not write.
2. Attach a `plan.warn(...)` for anything the user should know, especially
   when existing projects will break.
3. Add tests that assert both the plan's contents *and* that planning wrote
   nothing.
4. Wire it into the CLI (a `cmd_*` plus a subparser) and, if it belongs there,
   into `gui/controller.py`.

### Running the tests
```bash
python -m venv .venv && .venv/bin/pip install pytest
.venv/bin/pytest -q
```
Tests that need the installed KiCad libraries or `kicad-cli` skip when absent;
GUI tests skip when there is no display.

---

## 5. Naming conventions

Allowed characters: letters, digits, `_`, `-`, `.`, `+` — a superset of what
the official libraries use, so a legitimate KiCad name is never rejected.
`:` (the `lib_id` separator), path separators, leading/trailing dots, Windows
reserved names (`CON`, `NUL`, `COM1`, …) and names over 100 characters are
refused. Names differing only by case cannot coexist on Windows or macOS and
are an error.

* **Categories** — manufacturer or functional domain: `<Vendor>-<Family>` or
  `<Function>_<Subtype>`.
  * *Good*: `TI-TPAxxx_AUDIO-AMP`, `Passives_Inductors_Sagami`, `Connector_XT`
  * *Bad*: `mylib`, `temp`, `new_parts`, `3255` (meaningless on its own)
  * *Bad — collides with an official KiCad library*: `MCU_RaspberryPi`,
    `Connector`, `Audio`, `Amplifier_Audio`. A colliding nickname makes
    `lib_id` resolution depend on global table order, which differs per
    machine. See §7.5; prefix personal categories if in doubt
    (`AX_MCU_RaspberryPi`).
* **Symbols** — exact manufacturer part number, and the filename stem must
  match the internal symbol name: `TPA3255DDV.kicad_sym`, `RP2040.kicad_sym`.
* **Footprints** — IPC-7351 or manufacturer package names:
  `SOP63P810X120-44N.kicad_mod`, `JST_B4B-ZR_LF__SN_.kicad_mod`. The filename
  stem must match the internal footprint name, because KiCad resolves a
  `lib_id` by filename.
* **3D models** — named after the **footprint** they belong to, lowercase
  extension: `SOP63P810X120-44N.step`. A model belongs to a footprint, not to
  a symbol, and the packager finds it by reading the footprint's own `(model …)`
  path. Naming it after the symbol is why the old packager silently shipped
  bundles with no 3D models.

---

## 6. Konnect MCP integration
If the `konnect` MCP server is active:
* Route schematic queries and placement commands through Konnect MCP tools
  (`sch_components`, `sch_wiring`).
* Do **not** inject raw text into a live `.kicad_sch` or `.kicad_pcb`. The
  tooling in this repo only ever *reads* project files — that is the
  packager's job — and never edits them.

---

## 7. KiCad 10 on-disk facts (verified, do not re-derive)

Measured against **KiCad 10.0.6** on this machine: all 22 784 files in
`/usr/share/kicad/symbols/*.kicad_symdir/`, plus `kicad-cli` behaviour probes.
These are encoded as tests in `tests/test_kicad_conventions.py`, which skip
when KiCad is not installed. **Re-run those tests rather than re-measuring.**

### 7.1 Symbol library layout
| Fact | Measurement |
|---|---|
| A symbol library is a **directory** `<Nick>.kicad_symdir` | 224 directories, 0 loose `.kicad_sym` at the symbols root |
| **Exactly one top-level `(symbol …)` per file** | 22 784 / 22 784. No exceptions. |
| Internal symbol name == filename stem | 22 783 / 22 784 |
| …the single exception | `Interface_USB.kicad_symdir/tusb564.kicad_sym` declares `(symbol "TUSB564")` — a **case-only** difference |
| Filename character set | alphanumerics plus `+ - . _` only |

**Consequences for tooling:**
- A multi-symbol `.kicad_sym` (vendor bundle, or a project cache library) MUST be
  **split into one file per symbol** on ingest. Keeping it whole produces a file
  unlike anything KiCad ships. Name each file after its internal symbol name.
- Compare filename stem to internal name **case-insensitively**. An exact-match
  assumption rejects a file KiCad itself ships. A case-only difference is a
  warning, never an error.
- `naming.py`'s allowed set (`A-Za-z0-9_-.+`) is a superset of what the official
  libraries use, so it can never reject a legitimate KiCad name.

### 7.2 Derived symbols (`extends`) — cross-file, not in-file
Of 12 249 derived symbols in the official libraries:
- **0** have their parent defined in the same file.
- **0** reference a parent outside their own `.kicad_symdir`.

So `(extends "PARENT")` **always** names a *sibling file* in the same symdir:
```
Diode_Bridge.kicad_symdir/B40R.kicad_sym      <- parent, full definition
Diode_Bridge.kicad_symdir/B250R.kicad_sym     <- (extends "B40R"), properties only
```
**Consequence:** renaming or moving a symbol must scan **every sibling file in
the symdir** for `(extends "<old>")`. An in-file-only rewrite silently breaks
derived symbols. Moving a symbol out of a symdir breaks any sibling that
extends it — detect and refuse, or move the whole family.

### 7.3 Unit sub-symbols
Nested one level below the top-level symbol and named `<Parent>_<unit>_<style>`:
```
(symbol "LM358"            ->  (symbol "LM358_0_1"
                               (symbol "LM358_1_1"
```
**Consequence:** `rename_symbol` is a multi-site edit — the top-level name, every
`_<unit>_<style>` child, the `Value` property when it equals the old name, the
filename, and sibling `extends` references.

### 7.4 `kicad-cli` as a validator — asymmetric, read carefully
| Command | Corrupt input | Good input | Verdict |
|---|---|---|---|
| `fp export svg -o <dir> <lib.pretty>` | exit **2** | exit 0 + one SVG per footprint | **Trustworthy** by exit code |
| `sym export svg -o <dir> <lib.kicad_symdir>` | exit **0**, writes nothing | exit 0 + one SVG per unit | **NOT trustworthy** by exit code |
| `sym upgrade <lib.kicad_symdir>` | exit **2** | exit 0 | Trustworthy, but **rewrites files in place** |

**Consequences for `check.py`:**
- Footprints: use `fp export svg`, exit code is the signal. The output directory
  must already exist — `kicad-cli` will not create it and reports
  `Error creating svg file` while still exiting 0.
- Symbols: either compare the produced SVG count against the expected symbol
  count, or run `sym upgrade` **on a throwaway copy**. Never run `sym upgrade`
  against the real library.
- `kicad-cli` remains optional; skip these checks silently when it is absent.

### 7.5 Nickname collisions with official libraries
224 official symbol nicknames, 155 footprint nicknames. Checked examples:

| Candidate | Collides? |
|---|---|
| `MCU_RaspberryPi` | **YES** — official symbol library |
| `Connector`, `Audio`, `Amplifier_Audio` | **YES** |
| `Connector_XT`, `TI-TPAxxx_AUDIO-AMP`, `Passives_Inductors_Sagami` | no |

A colliding nickname makes `lib_id` resolution depend on global-table ordering,
which differs between machines. Validate new category names against
`$KICAD10_SYMBOL_DIR` / `$KICAD10_FOOTPRINT_DIR`, falling back to
`/usr/share/kicad/{symbols,footprints}`, and warn on collision.

> The §5 example `MCU_RaspberryPi` in this document is therefore a **bad**
> example. Prefer a personal prefix, e.g. `AX_MCU_RaspberryPi`.
