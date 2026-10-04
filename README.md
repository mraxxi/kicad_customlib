# KiCad Custom Library (`KICAD_CUSTOM_LIB`)

A portable custom component library for KiCad 10 — symbols, footprints and 3D
models — plus a stdlib-only Python tool that imports vendor downloads, keeps
every cross-reference correct, and exports lean per-project bundles.

---

## Why it is built this way

**The disk is the source of truth.** There is no parts database. The tool
scans the three directory trees and derives every relationship from the files
themselves: a symbol points at a footprint through its `Footprint` property, a
footprint points at its 3D models through their `(model …)` paths. Nothing can
drift out of sync with a database, because there is no database to drift from.

**Plan, then apply.** Nothing is written directly. Every operation first
builds a plan — which files will be created, which references rewritten, what
will break — and shows it to you. Only then does it run. `--dry-run` stops at
the plan; without `--yes` you are asked first.

---

## Layout

```
KICAD_CUSTOM_LIB/
├── sym-lib-table               # master symbol table   (generated)
├── fp-lib-table                # master footprint table (generated)
├── provenance.json             # where each file came from (advisory)
├── symbols/
│   └── <Category>.kicad_symdir/<Symbol>.kicad_sym
├── footprints/
│   └── <Category>.pretty/<Footprint>.kicad_mod
├── 3dmodels/
│   └── <Category>.3dshapes/<Model>.step
├── template/                   # project templates, page layouts
├── staging-temp/               # local scratchpad, not in git
│   ├── intake/<Category>/      # drop downloads here for batch import
│   └── imported_archive/       # processed items land here
├── scripts/
│   ├── lib_manager.py          # the CLI and GUI entry point
│   └── src/{core,gui}/
└── tests/
```

A category's three directories always share one base name. A directory only
exists once it holds a file — git cannot track an empty directory, so an empty
category would exist on one machine and not the other.

---

## Setup on a new machine

### 1. Clone
```bash
git clone https://github.com/mraxxi/kicad_customlib.git /path/to/KICAD_CUSTOM_LIB
```

### 2. Tell KiCad where it is
**Preferences → Configure Paths…** → add:

| Name | Path |
|---|---|
| `KICAD_CUSTOM_LIB` | the absolute path to this folder |

Every path inside the library is written relative to this variable, which is
why the repository works unchanged on Linux, Windows and macOS.

### 3. Register the master tables
* **Preferences → Manage Symbol Libraries… → Global Libraries** — add
  `KICAD_CUSTOM_LIB/sym-lib-table`.
* **Preferences → Manage Footprint Libraries… → Global Libraries** — add
  `KICAD_CUSTOM_LIB/fp-lib-table`.

### 4. Check it over
```bash
python scripts/lib_manager.py check
```
Exits non-zero if anything is actually broken, and tells you how to fix each
finding.

### After pulling on the other machine
```bash
python scripts/lib_manager.py sync pull     # fast-forward, then audit
```
`sync pull` runs `check` on what arrived, which is the point: a part committed
on one machine without regenerating the tables looks fine there and fails to
load here. If it reports `table-stale`:
```bash
python scripts/lib_manager.py generate --yes
```

---

## Everyday use

### See what is in there
```bash
python scripts/lib_manager.py list          # category totals
python scripts/lib_manager.py list -v       # every item and its references
```

### Import a vendor download
Works with a ZIP, a folder, or a single `.kicad_sym` / `.kicad_mod` / `.step`.

```bash
python scripts/lib_manager.py ingest ~/Downloads/tpa3255.zip \
    -c TI-TPAxxx_AUDIO-AMP --dry-run      # look first
python scripts/lib_manager.py ingest ~/Downloads/tpa3255.zip \
    -c TI-TPAxxx_AUDIO-AMP --yes          # then do it
```

What it does for you:

* finds **every** symbol, footprint and model in the bundle, not just the first;
* skips macOS `__MACOSX/._*` resource forks and `.DS_Store`, and refuses
  archive members that try to escape the extraction directory;
* splits a multi-symbol `.kicad_sym` into one file per symbol, which is what
  every official KiCad library does;
* sanitises names that would be illegal on Windows —
  `Inductor_2*10uH_Leaded_7W15` becomes `Inductor_2_10uH_Leaded_7W15`;
* names each 3D model after its **footprint**, and rewrites the footprint's
  model path to `${KICAD_CUSTOM_LIB}/…`, creating the `(model …)` block if the
  vendor shipped none;
* sets each symbol's `Footprint` property to `<Category>:<Footprint>`;
* reports anything it could not pair instead of guessing.

Useful flags: `--select NAME …` to import only some items,
`--conflict skip|overwrite|rename` (default: skip with a warning).

### Batch import
Drop downloads into `staging-temp/intake/<Category>/` and run:
```bash
python scripts/lib_manager.py sync-staging --archive
```
Each item is independent — one failure is reported and the rest continue. With
`--archive`, imported items move to `staging-temp/imported_archive/`.

### Rename and reorganise
These rewrite references across the **whole** library, including symbols in
other categories and `(extends …)` in sibling symbol files.

```bash
python scripts/lib_manager.py rename symbol    TI-TPAxxx_AUDIO-AMP:TPA3255DDV TPA3255DDVR
python scripts/lib_manager.py rename footprint TI-TPAxxx_AUDIO-AMP:SOP63P810X120-44N SOP65P810X120-44N --update-model-file
python scripts/lib_manager.py rename model     TI-TPAxxx_AUDIO-AMP:old.step amp_body.step
python scripts/lib_manager.py rename category  3255 TI-TPAxxx_AUDIO-AMP
python scripts/lib_manager.py move   symbol    Conn_XT:XT60 --to Connector_XT
```

> Renaming anything breaks projects that already reference the old name. The
> plan says so before you confirm. Afterwards, in the affected project, use
> **Tools → Edit Symbol Library References** (or **Change Footprints**).

### Export a project bundle
```bash
python scripts/lib_manager.py package ~/projects/amp
python scripts/lib_manager.py package ~/projects/amp --out ~/projects/amp/libs/vendor
```
Copies only the custom parts the project actually uses into a self-contained
`${KIPRJMOD}` bundle, so the project can be published without the whole
library. It resolves each 3D model by reading the footprint's own path, carries
a derived symbol's parent along even though the project never names it, merges
the project's `sym-lib-table` / `fp-lib-table` at the **project root** without
discarding rows you configured by hand (backing them up first), and lists
separately everything it could not provide — normally parts from KiCad's own
libraries.

### Sync between the two machines

```bash
python scripts/lib_manager.py sync status            # --json for scripts
python scripts/lib_manager.py sync fetch
python scripts/lib_manager.py sync pull              # fast-forward, then audit
python scripts/lib_manager.py sync commit --yes      # message from the diff
python scripts/lib_manager.py sync push              # audits first
```

`sync status` reduces the repository to one word — `in_sync`, `ahead`,
`behind`, `diverged`, `dirty`, `detached`, `no_upstream`, `unmerged`,
`operation_in_progress` — lists the incoming and outgoing commits, and says
which actions are currently unavailable and why.

`sync commit` writes a message describing what actually changed (`Add 3
symbols to Conn_XT`, `Regenerate master library tables`) rather than "Update
files"; `-m` overrides it. `sync push` runs the audit first and refuses on
errors; `--skip-check` overrides that, for parking work in progress on the
remote.

**What it will not do.** There is no force push, reset, checkout, stash,
rebase, explicit merge, clean or gc anywhere in the tool — every one of those
can discard work that exists in no other clone, which on a two-machine library
means losing it outright. A diverged branch or a conflict is reported, with
the reason, and left for a terminal.

The reported fetch age matters: ahead/behind is counted against the
remote-tracking ref, so the numbers are only as current as the last `fetch`.

---

## The desktop app

```bash
python scripts/lib_manager.py          # or: lib_manager.py gui
```

* **Browser** — categories on the left; symbols, footprints or 3D models on
  the right with live search and sortable columns. Rows with a problem are
  underlined. Right-click for rename, move, copy name, open folder, delete.
* **Import…** — add any number of ZIPs, folders or files; every candidate
  appears in an editable table with its own name and category; **Preview
  changes** shows the exact plan; the import runs in the background with a
  progress bar and a log.
* **Audit** — the same findings as `check`, filterable by severity, with the
  suggested fix for the selected one. Double-click to jump to the item.
* **Package project…** — pick a project, see what will and will not be
  bundled, then run it.
* A banner appears whenever the master tables fall out of date.

### The git strip and the sync view

Under the toolbar, always visible when the library is a git repository:

```
main · b6c9373 · ↑2 ↓0 · 3 changed · fetched 4 min ago        [Sync…]
```

`Sync…` (or `Ctrl+R`) opens the detail: the upstream and its URL, HEAD, the
incoming and outgoing commits, the working tree grouped by status, a commit
message prefilled from the diff, and git's raw output in a copyable log.

* Fetch · Pull · Commit · Commit & Push · Push. **A disabled action always
  says why**, in a sentence next to the button — "commit them first", "the
  branch has diverged, reconcile it in a terminal".
* The audit runs as soon as the view opens. Errors hold the push back behind
  an explicit `Push anyway (N error(s))`, never remembered between openings.
* A pull re-scans the library, refreshes the browser and re-runs the audit.
* Every git call runs on a worker thread, one at a time, so the window stays
  responsive and two git commands never contend for the index lock.
* Quitting with uncommitted library changes asks first, offering
  *Commit & Push* / *Quit anyway* / *Cancel* — the classic two-machine
  failure is leaving parts behind on the machine you walked away from.

Shortcuts: `Ctrl+I` import, `F2` rename, `Del` delete, `Ctrl+F` search,
`F5` refresh, `Ctrl+R` sync, `Ctrl+Q` quit.

Window sizes, the panel split, column widths, the sort and the last category
are remembered per screen resolution, so attaching a second monitor does not
inherit a layout sized for the first. *Library → Reset window layout* clears
them. Preferences live in `~/.config/kicad_customlib/gui.json`, outside the
repository.

On Linux the file dialogs use `zenity` if it is installed, then `kdialog`,
then Tk's own — Tk's is not native there, which is why it looks nothing like
the rest of the desktop. **zenity is preferred even on KDE**, which is the
opposite of what you would expect: on this machine `kdialog` took a median of
18.5 s to show its window over five runs, against zenity's 0.4 s every time.
If yours behaves better, `KICAD_CUSTOMLIB_FILEPICKER=kdialog` forces the
Plasma dialog; `tk` and `zenity` pin the others.

The picker never blocks the interface — the window keeps repainting and
resizing while a dialog is open, however slow the helper is.

Colours come from the desktop's own scheme where `kdeglobals` can be read.

If Tk is missing the app says how to install it (`pacman -S tk`,
`apt install python3-tk`, …). Everything is available from the CLI regardless.

---

## Where provenance fits

`provenance.json` records only what the files cannot tell you: the original
vendor filename, which download it came from, and the date. Keys are
`<Category>/<kind>/<name>`, sorted, with no global timestamp — so an unchanged
save is byte-identical and never causes a spurious git conflict between
machines. It is advisory: the library works perfectly without it, renames keep
it in step, and `check` reports entries whose files have disappeared.

A corrupt `provenance.json` is a loud error that leaves the file untouched,
never a silent reset.

Upgrading from the old `manifest.json`:
```bash
python scripts/lib_manager.py migrate-manifest
```
Records with no files on disk behind them are dropped rather than carried
forward. The old file is left in place for you to delete.

---

## Adjusting 3D model alignment

1. Open the footprint in KiCad's **Footprint Editor**.
2. `E` (Properties) → **3D Models**.
3. Adjust offset and rotation until the model sits on the pads.
4. Save.

The offsets live in the `.kicad_mod`, so they apply everywhere that footprint
is used — and the tooling only ever replaces the model *path*, so your
alignment survives renames, category moves and packaging.

---

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

Core and GUI are standard-library only; `pytest` is dev-only and
`tkinterdnd2` is an optional extra for drag-and-drop. Tests that need the
installed KiCad libraries or `kicad-cli` skip when they are absent, and the
GUI tests skip without a display.

`AGENTS.md` documents the architecture, the invariants, and the facts measured
from KiCad 10's own libraries. Read it before changing anything under
`scripts/`.
