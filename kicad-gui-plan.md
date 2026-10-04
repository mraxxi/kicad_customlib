# Work Plan: GUI polish and git integration (`KICAD_CUSTOM_LIB`)

Audience: an LLM coding agent working inside the repo `mraxxi/kicad_customlib`.

Read this whole file, then `AGENTS.md` (especially §1 Architecture and §7 KiCad 10
on-disk facts), before changing anything. Work phase by phase. Commit after each
phase. Do not start a phase until the previous phase's acceptance checks pass.

This plan follows the completed work described in `CHANGELOG.md` (Phases 0–4 of
`kicad-customlib-plan.md`). The core engine, CLI and GUI already exist and
474 tests pass. **Nothing here requires changing `scripts/src/core/` except by
adding one new module (`core/vcs.py`).**

---

## 0. Context

### 0.1 What this repo is

A personal KiCad 10 custom library (symbols, footprints, 3D models) synced
between two machines with git, plus a stdlib-only Python tool
(`scripts/lib_manager.py`, CLI + Tkinter GUI).

### 0.2 Architecture rules you must not break

From `AGENTS.md`. These are load-bearing:

1. **The disk is the source of truth.** `core/library.py` scans and derives
   every relationship. No parts database.
2. **Plan, then apply.** Mutating a *library file* builds an `ops.Plan` (pure
   computation, touches nothing), which is shown to the user, and only then
   does `ops.apply()` run it.
3. **Stdlib only** for core and GUI. Optional extras must degrade gracefully
   when absent. `pytest` is dev-only.
4. **No absolute paths** in any library file or committed config.
5. **The GUI is a thin layer.** `gui/controller.py` holds every decision and
   imports no Tk; widgets only render and collect intent. This is what keeps a
   possible future Qt port cheap — **hold this line strictly**.
6. Must run on Linux (primary: KDE Plasma), Windows (secondary) and macOS
   (CI only).

### 0.3 Running the tests

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q                                   # 474 tests currently
.venv/bin/python scripts/lib_manager.py check          # must exit 0
```

Tests needing the installed KiCad libraries or `kicad-cli` skip when absent.
GUI tests skip without a display, and skip on Windows CI (see
`tests/test_gui_widgets.py`).

### 0.4 Scope of this plan

| In scope | Out of scope |
|---|---|
| **Fixing plan-time provenance mutation (Phase 0 — a live bug)** | Part inventory / stock tracking (separate project) |
| Window/panel/column geometry persistence | Qt/PySide port |
| File dialog behaviour and native dialogs | Changing the core library engine |
| Combobox appearance and the category picker | Writing GUI state into KiCad files |
| A git status strip and sync view in the GUI | Any git operation that can lose work |
| Small UX polish (§5) | Icon themes, custom widget drawing |

---

## 1. Decisions already made — do not re-litigate

| Decision | Choice |
|---|---|
| Toolkit | **Stay on Tkinter/ttk.** Polish it rather than port to Qt. Keep `controller.py` toolkit-free so a port stays possible later. |
| Inventory / stock tracking | **Dropped.** It will be a separate project. Do not add quantity, BOM or purchasing features. |
| Geometry profile keying | Keyed on `{screen_width}x{screen_height}@{dpi}` so a monitor change yields a separate profile. |
| First-run import directory | **`<lib_root>/staging-temp/intake/`**, falling back to `<lib_root>` if it does not exist. |
| Dropdown problems to fix | **Both** — the unthemed popup listbox *and* the `＋ New category...` sentinel entry. |
| Pre-push audit | Run `check` before push; on errors, **block by default but offer an explicit override**. This is a single-user multi-machine setup, so the user is allowed to overrule the tool knowingly. |
| Git scope | Fetch, pull (`--ff-only` only), commit, push. Nothing else. |
| Plan/apply for git | **Do not** force git operations into `ops.Plan`. See §3.4. |
| Settings file corruption | GUI preferences fail **soft** (fall back to defaults). This is deliberately unlike `provenance.json`, which fails loud. See §2.2. |

---

## 2. Verified platform constraints

These were measured or confirmed on the target machine (Arch Linux, KDE
Plasma, Wayland session with XWayland, Python 3.14, Tk present). **Treat them
as given; re-deriving them wastes time.**

### 2.1 Window position may not restore

The target runs a Wayland session (`WAYLAND_DISPLAY=wayland-0`) with
XWayland (`DISPLAY=:1`). Tk is X11-only and therefore runs under XWayland.

* Restoring window **size** is reliable.
* Restoring window **position** is best-effort: XWayland often honours it,
  native Wayland never does.

**Requirement:** always restore size; attempt position; clamp it to the
current screen; never treat a failed position as an error, and never log a
scary message about it.

### 2.2 `ttk.Treeview` column widths fight `stretch`

`SortableTree.set_columns()` (`scripts/src/gui/widgets.py`) currently sets
every column to `width=150, stretch=True` (and the first to `width=240`).
With `stretch=True`, Tk resizes columns with the window and **overrides any
restored width**.

**Requirement:** set `stretch=False` on all columns except one designated
filler column (the last one — `Source` for symbols/footprints, `Source` for
models). Accept the consequence that other columns no longer grow with the
window; that is the correct trade when widths are user-controlled.

### 2.3 `PanedWindow.sashpos()` is ignored before the pane is mapped

Setting `sashpos(0, n)` before the widget has been mapped and sized clamps to
zero. It must be applied from a `<Map>` binding or `after_idle`, after
`update_idletasks()`.

Also note: in `scripts/src/gui/browser.py:48` the paned window is a **local
variable** `paned`. It must become `self.paned` to be addressable.

### 2.4 `tkinter.filedialog` is not native on Linux

On Linux, Tk uses its own Motif-era file dialog, not a GTK or Qt one. No
amount of ttk theming changes it. `kdialog` (ships with Plasma) and `zenity`
can provide a genuinely native dialog — see §4.2.

On **Windows**, Tk's dialog *is* the native one. Do not replace it there.

### 2.5 `ttk.Combobox`'s popup is an unthemed Tk listbox

The dropdown list is a plain `tk.Listbox`, outside the ttk theme. It is styled
through the option database, e.g. `*TCombobox*Listbox.background`, and not via
`ttk.Style`.

### 2.6 git subprocesses will hang on a credential prompt

A GUI has no terminal attached. If git asks for HTTPS credentials the
subprocess blocks forever. **Every** git invocation must set
`GIT_TERMINAL_PROMPT=0` and an empty `GIT_ASKPASS` so it fails fast with a
readable error instead.

### 2.7 Windows HiDPI blur

`widgets.apply_scaling()` only handles the Linux/X11 path (`tk scaling` from
DPI). On Windows, Tk renders blurry on a HiDPI display unless
`ctypes.windll.shcore.SetProcessDpiAwareness(1)` is called **before** the Tk
root is created.

---

## 3. Current GUI state

```
scripts/src/gui/
  controller.py     411 lines  view-model, no Tk. 18 public methods.
  widgets.py        331 lines  CategoryPicker, PlanPreview, StatusBar,
                               SortableTree, apply_scaling, modal
  browser.py        188 lines  category list + item table
  import_dialog.py  489 lines  multi-source import with worker thread
  rename_dialog.py  231 lines  _PlanDialog base, Rename/Move/CategoryRename
  app.py            501 lines  LibraryManagerApp shell + AuditDialog
```

### 3.1 Things this plan will touch, with current locations

| What | Where | Current behaviour |
|---|---|---|
| Config load/save | `app.py:31-50` (`CONFIG_DIR`, `CONFIG_FILE`, `_load_config`, `_save_config`) | Module-level helpers; stores only `last_category`. Path: `~/.config/kicad_customlib/gui.json` |
| Main window size | `app.py:58-59` | Hardcoded `1180x720`, `minsize(900, 560)` |
| Paned window | `browser.py:48-62` | Local `paned`, weights 1 / 3, no sash persistence |
| Column widths | `widgets.py` `SortableTree.set_columns` | Hardcoded 150 / 240, `stretch=True` |
| File dialogs | `app.py:363`; `import_dialog.py:162, 170, 175` | No `initialdir` passed anywhere |
| Comboboxes | `widgets.py:95` (CategoryPicker); `rename_dialog.py:141, 185`; `import_dialog.py:147, 352` | Default styling, small popup |
| Category sentinel | `widgets.py:19, 106, 109, 179` (`NEW_CATEGORY_SENTINEL`) | `＋ New category...` as a fake list entry |
| Status bar | `app.py:116-117`, `widgets.StatusBar` | Message + right-aligned counts |
| Stale-tables banner | `app.py:88-93`, packed `after=self.toolbar` | Shown when tables are out of date |
| Other dialog sizes | `app.py:394`; `rename_dialog.py:29`; `import_dialog.py:46`; `widgets.py:198` | Hardcoded |

### 3.2 Existing tests to extend

* `tests/test_gui_controller.py` — 43 headless tests of the view-model. **New
  logic belongs here.**
* `tests/test_gui_widgets.py` — 38 Tk tests, one interpreter per test, skipped
  on Windows CI and without a display.
* `tests/kicad_fixtures.py` — builders for KiCad files, vendor ZIPs and
  projects (`write_project`).
* `tests/conftest.py` — `lib_root`, `populated_lib`,
  `filesystem_is_case_insensitive`.

---

## 4. Phases

### Phase 0 — Fix plan-time provenance mutation (do this first)

**This is a live bug in shipped code, not a new feature. It loses provenance
data on every rename performed through the GUI. Fix it before anything else.**

#### 0.1 The bug

`refactor.plan_rename_symbol/footprint/model/category` and
`ingest.plan_ingest` mutate the passed `Provenance` object **while building
the plan** (`scripts/src/core/refactor.py:255, 325, 336, 385, 450, 476, 495,
569` and `scripts/src/core/ingest.py:445, 479, 511`).

That breaks the architecture's own Rule 2: building a plan must touch nothing.
It is mostly harmless from the CLI, which builds a plan once. The GUI turns it
into data loss, because `gui/rename_dialog.py:146, 223` rebuild the plan on
**every keystroke** of the new-name entry, to keep the live preview current.

Reproduction (verified):

```python
prov.record("Amp_Test", "symbol", "TPA3255DDV", original_name="vendor.kicad_sym")
for i in range(1, len("TI_Amps") + 1):                 # what typing does
    rf.plan_rename_category(root, "Amp_Test", "TI_Amps"[:i], prov=prov)

# before: ['Amp_Test/symbol/TPA3255DDV']
# after:  ['T/symbol/TPA3255DDV']          <-- stranded at the first keystroke
# prov.get("TI_Amps", "symbol", "TPA3255DDV") -> None
```

The first keystroke moves the key to `T/...`; every later keystroke finds
nothing under `Amp_Test` and does nothing. `controller.apply()` then persists
the damaged file.

The same applies to `ImportDialog`: `_grouped_plan()` is called by both
**Preview changes** and **Import** (`import_dialog.py:411, 417`), so previewing
and then cancelling still records provenance for items that were never
imported.

Real-world evidence from the owner's library — every one of these is a
partially-typed category name left behind by the live preview:

```
CUSTOM_G/footprint/RCJ-045_RCA
CUSTOM_GENERIC-AUDIO-CONN/symbol/Inductor_2_10uH_Leaded_7W15
TPAxxxx_TI-/symbol/TPA3255DDV
_Generic-Heatsinks/symbol/Clip-on_Heatsink
```

Blast radius is **provenance metadata only** — original vendor filename,
source and import date. No library file is corrupted, which is why
`check` reports 0 errors. But the metadata is genuinely lost.

#### 0.2 Why the tests missed it

`tests/test_refactor.py::test_planning_a_refactor_writes_nothing` compares
**file bytes on disk** before and after planning, and never passes a `prov`
object at all. The in-memory mutation was therefore invisible to it.

#### 0.3 The fix

Make provenance updates part of **apply**, not plan.

Add a deferred-edit record that rides along on the plan, alongside the
warnings, notes and conflicts it already carries:

```python
# core/ops.py
@dataclass(frozen=True)
class ProvenanceEdit:
    action: str                       # "record" | "rename" | "forget" | "rename_category"
    category: str = ""
    kind: str = ""
    name: str = ""
    new_name: str = ""
    new_category: str = ""
    original_name: str = ""
    source: str = ""

@dataclass
class Plan:
    ...
    provenance: list[ProvenanceEdit] = field(default_factory=list)
```

* `plan_*` functions append `ProvenanceEdit`s instead of calling `prov.*`.
  They may still *read* provenance.
* A single applier — `provenance.apply_edits(prov, edits)` — runs them, called
  only after `ops.apply()` succeeds, from `controller.apply()` and
  `lib_manager._run_plan()`.
* `Plan.extend()` must carry `provenance` across, like the other fields.
* Prefer dropping the `prov=` parameter from the `plan_*` signatures entirely,
  so the old mistake is unrepresentable. Update the call sites in
  `gui/controller.py` and `scripts/lib_manager.py`.

#### 0.4 Clean up the damage

Add a way to drop stale entries, and wire it in:

```
lib_manager.py prune-provenance [--dry-run]
```

It removes entries whose `<Category>/<kind>/<name>` matches nothing on disk,
reporting each one. Offer the same from the audit dialog.

**Second, smaller bug while you are here:** `check.py`'s `provenance-stale`
remedy claims the entry "will be dropped on the next write". Nothing prunes
it — `Provenance.save()` writes `self.items` verbatim, so stale keys
accumulate forever. Either make the prune automatic on save (and say so) or
correct the message to point at `prune-provenance`. Do not leave the text
claiming something that does not happen.

#### Acceptance checks — Phase 0

* A test that builds every `plan_*` **with** a `Provenance` object and asserts
  the object is byte-identical afterwards (`to_json_text()` unchanged). This is
  the test whose absence allowed the bug.
* A test that replays the keystroke sequence above and asserts the provenance
  entry ends up under the final name, with `original_name` and `source` intact.
* A test that previewing an import and then *not* applying leaves provenance
  untouched.
* A test that applying a rename *does* move the provenance key, so the
  deferred path actually runs — guard against "fixed" by simply deleting the
  updates.
* `prune-provenance` removes only entries with nothing on disk, keeps the
  rest, and is a no-op on a clean library.
* Full suite green; `check` exits 0.
* Run `prune-provenance` against the real library afterwards and confirm the
  15 stale warnings go away.

---

### Phase 1 — Settings and geometry persistence

**Goal:** the window, panel split and column widths come back as the user left
them, per screen configuration.

#### 1.1 New module `scripts/src/gui/settings.py`

Move the config helpers out of `app.py` (browser and widgets both need them)
and give them structure.

```python
SCHEMA_VERSION = 1

def config_path() -> Path                 # ~/.config/kicad_customlib/gui.json
                                          # honour $XDG_CONFIG_HOME when set

class Settings:
    data: dict

    @classmethod
    def load(cls) -> "Settings"           # never raises; bad file -> defaults
    def save(self) -> None                # atomic write, never raises
    def get(self, key, default=None)
    def set(self, key, value) -> None

    # --- geometry ---
    @staticmethod
    def profile_key(widget) -> str        # f"{screen_w}x{screen_h}@{dpi}"
    def profile(self, key) -> dict        # created on demand
    def reset_layout(self) -> None        # clears every geometry profile

    # --- remembered directories ---
    def dir_for(self, purpose: str, default: Path) -> Path
    def remember_dir(self, purpose: str, path: Path) -> None
```

On-disk shape:

```jsonc
{
  "version": 1,
  "last_category": "TPAxxxx_TI-AUDIO-AMP",
  "dirs": {
    "import_source":   "/home/u/Downloads",
    "package_project": "/home/u/projects/amp",
    "package_out":     "/home/u/projects/amp/project_libs"
  },
  "geometry": {
    "3840x1080@96": {
      "windows": { "main": "1180x720+120+80", "import": "1040x620",
                   "audit": "1000x560", "sync": "900x640" },
      "sash":    { "browser": 310 },
      "columns": {
        "symbol":    { "Name": 240, "Category": 150, "Footprint": 260,
                       "3D": 50, "Source": 180 },
        "footprint": { },
        "model":     { }
      },
      "browser": { "kind": "symbol", "category": null,
                   "sort": ["Name", false] }
    }
  }
}
```

**Corruption policy — deliberately different from `provenance.json`.** A
damaged preferences file must *not* stop the GUI from opening: log nothing
alarming, fall back to defaults, and overwrite on next save. Preferences are
disposable; library data is not. Write a test asserting this, and a comment
in the module explaining the asymmetry so nobody "fixes" it later.

#### 1.2 Restore and persist geometry

* `app.py` — on startup, read the profile for the current screen; apply
  `minsize` first, then the saved size; attempt the saved position with
  clamping (see §2.1). If no profile exists, keep the present defaults.
* Clamping rules: width/height capped to the screen; if the restored position
  would put the title bar off-screen, drop the position and keep the size.
* **Save on a debounce.** Bind `<Configure>` on the root and save ~800 ms
  after the last event, plus an unconditional save from the
  `WM_DELETE_WINDOW` handler. Do not write on every event.
* Persist the same way for `ImportDialog`, `AuditDialog` and the new sync view
  (size only, no position).

#### 1.3 Sash position

* Rename `paned` to `self.paned` in `browser.py`.
* Restore `sashpos(0, n)` from an `after_idle`/`<Map>` callback (§2.3).
* Save on `<ButtonRelease-1>` on the paned window, through the same debounce.

#### 1.4 Column widths

* `SortableTree.set_columns()` grows an optional `widths: dict[str, int] | None`
  parameter, and sets `stretch=False` on every column except the last (§2.2).
* `browser.refresh_items()` passes the saved widths for the current kind.
* Save widths on `<ButtonRelease-1>` over the tree heading area, per kind.
* Also persist and restore the browser's `kind`, selected category, and sort
  column + direction. **Do not** persist the search box — a stale filter at
  launch is confusing.

#### 1.5 Reset

Add **Library → Reset window layout** which clears the geometry profiles and
tells the user the change takes effect on restart (or re-applies defaults
immediately if that is easy). Without an escape hatch, one bad saved geometry
traps the user.

#### Acceptance checks — Phase 1

* `pytest` green; `check` exits 0.
* Headless tests in `test_gui_controller.py` or a new `test_gui_settings.py`:
  * profile key format, and that two different screen signatures keep
    independent profiles;
  * a corrupt/empty/unreadable config yields defaults without raising;
  * `dir_for` returns the default when nothing is remembered, and the
    remembered value afterwards;
  * `reset_layout()` clears geometry but preserves `last_category` and `dirs`;
  * saving twice with no change produces identical bytes.
* Tk tests: resize the root, fire the debounce, assert the saved value;
  construct the app with a pre-seeded profile and assert the geometry applied;
  assert column widths survive a kind switch and back.
* Manual: resize window, drag the split, widen a column, quit, relaunch —
  everything returns. Repeat with a second monitor attached and confirm the
  first profile is untouched.

---

### Phase 2 — File dialogs and dropdowns

**Goal:** dialogs open in a sensible place and look native where possible;
comboboxes stop looking out of place.

#### 2.1 `initialdir` everywhere

Four call sites (§3.1). Each gets a purpose and a first-run default:

| Call site | Purpose key | First-run default |
|---|---|---|
| `import_dialog.py:162` Add ZIP(s) | `import_source` | `<lib_root>/staging-temp/intake/`, else `<lib_root>` |
| `import_dialog.py:170` Add folder | `import_source` | same |
| `import_dialog.py:175` Add file(s) | `import_source` | same |
| `app.py:363` Package project | `package_project` | `<lib_root>` |
| (future) package output | `package_out` | the chosen project directory |

After each successful selection, remember the containing directory. A
remembered directory that no longer exists falls back to the default.

#### 2.2 Native dialogs via `kdialog` / `zenity`

New module `scripts/src/gui/filepicker.py`, with a Tk-free decision layer so
the selection logic is testable:

```python
def available_backend() -> str          # "kdialog" | "zenity" | "tk"
def open_files(parent, title, initialdir, filters) -> list[Path]
def open_directory(parent, title, initialdir) -> Path | None
```

Rules:

* Backend order: `kdialog` → `zenity` → Tk's `filedialog`. Detect with
  `shutil.which`. **Windows and macOS always use Tk** (§2.4).
* An env var escape hatch, e.g. `KICAD_CUSTOMLIB_FILEPICKER=tk`, to force the
  fallback.
* Run the helper with a timeout; a non-zero exit means cancel, not an error.
  A crash, timeout or unparseable output falls back to Tk's dialog **once**
  for that call, and is remembered for the session so it does not retry
  repeatedly.
* Build filters from the same extension lists already used in
  `import_dialog.py`.
* `kdialog` returns newline-separated paths for multi-select; parse
  accordingly.

This is an optional external *binary*, not a Python dependency, so Rule 3
holds — but it must work with neither helper installed.

#### 2.3 Combobox appearance

* A `style.py` or an `apply_theme(root)` function in `widgets.py` that, once at
  startup:
  * selects a ttk theme explicitly — `clam` on Linux, `vista` on Windows,
    `aqua` on macOS, with a safe fallback if unavailable (**this alone is a
    visible improvement; no theme is currently selected at all**);
  * styles the combobox popup through the option database (§2.5):
    `*TCombobox*Listbox.font`, `.background`, `.foreground`,
    `.selectBackground`, `.selectForeground`;
  * sets a sane `Treeview` `rowheight` and adds an `odd`/`even` striping tag;
  * applies Tk named fonts (`TkDefaultFont`, `TkFixedFont`) consistently.
* Give every readonly combobox a larger `height` so the popup is not three
  cramped rows.

**Optional, decide while implementing:** read KDE's palette from
`~/.config/kdeglobals` (`[Colors:Window]` / `[Colors:View]` →
`BackgroundNormal=R,G,B`, `ForegroundNormal`, and `[General] AccentColor`)
with stdlib `configparser`, and derive the ttk colours and the striping from
it. This is the biggest available "looks like it belongs on my desktop" win
and needs no dependency. **Confirm the exact key names on the target machine
before relying on them**, and skip silently if the file or keys are missing.

#### 2.4 Replace the category-picker sentinel

* Remove `NEW_CATEGORY_SENTINEL` and the magic list entry.
* `CategoryPicker` becomes a readonly combobox **plus a `New…` button**, which
  opens the existing validated modal.
* Keep the live-validation behaviour and the `validate` callback contract
  unchanged (`controller.validate_new_category`).
* Update the tests that assert the sentinel is present
  (`test_gui_widgets.py`: `test_category_picker_lists_categories_and_a_create_entry`,
  `test_category_picker_never_returns_the_sentinel_as_a_name`) — the second
  becomes unnecessary once the sentinel is gone; replace it with one asserting
  `get()` returns `""` before a choice is made.

#### 2.5 Windows HiDPI

Call `SetProcessDpiAwareness(1)` on Windows before the root is created (§2.7),
guarded by `sys.platform` and `try/except` (it fails on older Windows).

#### Acceptance checks — Phase 2

* `pytest` green on Linux; the suite still skips cleanly on Windows CI.
* Headless tests: backend selection honours the env override, prefers
  `kdialog` when present, falls back when neither helper exists, and returns
  `[]`/`None` on cancel (fake the helper with a stub script in `tmp_path`).
* Tk tests: the category picker exposes a `New…` button and no sentinel entry;
  `apply_theme` runs without error and leaves a known theme active.
* Manual on KDE: every file dialog opens the Plasma dialog, starting in
  `staging-temp/intake/` on first use and in the last-used directory after.
* Manual: force `KICAD_CUSTOMLIB_FILEPICKER=tk` and confirm everything still
  works.

---

### Phase 3 — `core/vcs.py`

**Goal:** all git knowledge and every safety rule, with no Tk, fully tested
headlessly. **No GUI work in this phase.**

#### 3.1 Module placement

`scripts/src/core/vcs.py`, exported from `core/__init__.py`. It is core rather
than gui because the CLI uses it too, and it must survive a toolkit change.

#### 3.2 Data model

```python
@dataclass(frozen=True)
class Commit:
    sha: str           # full
    short: str         # abbreviated
    subject: str
    author: str
    date_iso: str
    date_relative: str

@dataclass(frozen=True)
class GitResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    @property
    def ok(self) -> bool

@dataclass
class RepoStatus:
    is_repo: bool
    branch: str | None                 # None when detached
    detached: bool
    head: Commit | None
    upstream: str | None               # e.g. "origin/main"
    remote_name: str | None
    remote_url: str | None
    ahead: int
    behind: int
    staged: list[str]
    modified: list[str]
    deleted: list[str]
    untracked: list[str]
    unmerged: list[str]
    operation: str | None              # "merge" | "rebase" | "cherry-pick"
    last_fetch: datetime | None
    @property
    def dirty(self) -> bool
    @property
    def state(self) -> str             # see 3.3
    def blockers(self) -> dict[str, str]   # action -> human reason
```

#### 3.3 Derived state

A single word for the strip, computed in this precedence order:

`not_a_repo` → `operation_in_progress` → `unmerged` → `detached` →
`no_upstream` → `diverged` (ahead and behind) → `behind` → `ahead` →
`dirty` → `in_sync`

#### 3.4 Operations — and why these are *not* `ops.Plan`

`ops.Plan` models filesystem operations (`CopyFile`, `WriteText`, …) and
exists because KiCad S-expressions are fragile and interlinked. A git commit
is none of those things, and forcing it into that shape would be
cargo-culting. **Use a dedicated preview type instead:**

```python
@dataclass
class CommitPreview:
    message: str
    files: list[tuple[str, str]]     # (status letter, path)
    warnings: list[str]
```

Functions:

```python
def read_status(root: Path) -> RepoStatus
def incoming(root: Path, limit: int = 50) -> list[Commit]   # git log HEAD..@{u}
def outgoing(root: Path, limit: int = 50) -> list[Commit]   # git log @{u}..HEAD
def fetch(root: Path) -> GitResult
def preview_commit(root, status, lib=None) -> CommitPreview
def commit(root: Path, message: str) -> GitResult           # stages tracked + untracked library paths
def pull_ff_only(root: Path) -> GitResult
def push(root: Path) -> GitResult
def suggest_commit_message(root, status, lib) -> str
```

#### 3.5 Hardening requirements

* A private `_run(root, *args, timeout)` used by everything, which sets:
  * `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=""` (§2.6);
  * `GIT_OPTIONAL_LOCKS=0` so a status read never takes the index lock;
  * `LC_ALL=C` so output parsing is locale-stable.
* Timeouts: 10 s for local reads, 120 s for `fetch`/`pull`/`push`. A timeout
  returns a `GitResult` with a clear message, never an exception escaping to
  the UI.
* `git status --porcelain=v1 -z` and `-z`-separated parsing, so paths with
  spaces or non-ASCII survive.
* Ahead/behind via `git rev-list --left-right --count HEAD...@{u}`.
* `last_fetch` from the mtime of `.git/FETCH_HEAD`.
* `operation` detected from the presence of `.git/MERGE_HEAD`,
  `.git/rebase-merge/`, `.git/rebase-apply/`, `.git/CHERRY_PICK_HEAD`.
* **Never** invoke: `push --force`, `reset`, `checkout`, `stash`, `rebase`,
  `merge` (other than the ff-only pull), `clean`, `gc`. Add a comment saying
  so, and a test asserting the module contains no such call.

#### 3.6 Blocking rules

`blockers()` returns an action → reason map. Reasons are shown verbatim in the
UI, so they must read as complete sentences.

| Action | Blocked when |
|---|---|
| `fetch` | not a repo; no remote |
| `pull` | not a repo; no upstream; detached; unmerged paths; operation in progress; working tree dirty; nothing to pull; diverged |
| `commit` | not a repo; nothing to commit; unmerged paths; operation in progress |
| `push` | not a repo; no upstream; detached; unmerged paths; operation in progress; dirty; nothing to push; behind or diverged |

`pull` blocks on a dirty tree deliberately: a fast-forward that touches a
modified file fails with a confusing message, so refuse early and say why.

#### 3.7 Generated commit messages

`suggest_commit_message()` turns the status plus a `library.Library` scan into
something useful rather than "Update files":

* new symbol/footprint/model files → `Add 3 symbols to Conn_XT`
* deletions, renames → `Remove …`, `Rename …`
* only `sym-lib-table`/`fp-lib-table` → `Regenerate master library tables`
* only `provenance.json` → `Update provenance`
* mixed → a short summary line plus a bulleted body

Always editable in the UI. This is a convenience, not a policy.

#### 3.8 CLI parity

Add to `scripts/lib_manager.py`:

```
lib_manager.py sync status [--json]
lib_manager.py sync fetch
lib_manager.py sync pull                  # --ff-only semantics
lib_manager.py sync commit [-m MSG]       # plan/confirm as other commands do
lib_manager.py sync push [--skip-check]   # pre-push audit, override flag
```

Follow the existing conventions: print the preview, confirm unless `--yes`,
refuse to act non-interactively without `--yes`, non-zero exit on failure.

#### Acceptance checks — Phase 3

Build fixtures that create a temp repo plus a bare "remote"
(`git init --bare`), so no network is needed. New `tests/test_vcs.py` must
cover every state:

* not a repo; clean and in sync; ahead; behind; diverged; dirty (modified,
  deleted, untracked separately); unmerged/conflicted; detached HEAD;
  no upstream; mid-merge.
* `state` returns the right word for each, in the documented precedence.
* `blockers()` blocks exactly the right actions per state, with non-empty
  reasons.
* `pull_ff_only` succeeds when fast-forwardable and fails cleanly when
  diverged, without creating a merge commit.
* `push` sends outgoing commits; after it, `ahead == 0`.
* paths with a space and a non-ASCII character survive status parsing.
* a credential prompt cannot hang: assert the env hardening is applied
  (e.g. by invoking against an unreachable `https://` remote and asserting a
  fast non-zero result rather than a hang — keep the timeout short).
* `suggest_commit_message` produces the expected text for: a new symbol, a
  regenerated table only, and a mixed change.
* a grep-style test asserting no forbidden git subcommand appears in
  `vcs.py`.
* CLI tests via `lib_manager.main(argv)`, mirroring `tests/test_cli.py`.

`check` must still exit 0, and the whole suite green.

---

### Phase 4 — Git UI

**Goal:** glanceable state always visible; detail and actions on demand.
Thin layer over Phase 3.

#### 4.1 Controller additions

Extend `gui/controller.py` (still no Tk):

```python
def git_status(self, *, refresh: bool = False) -> vcs.RepoStatus
def git_incoming(self) -> list[vcs.Commit]
def git_outgoing(self) -> list[vcs.Commit]
def git_fetch(self) -> vcs.GitResult
def git_preview_commit(self, message: str | None = None) -> vcs.CommitPreview
def git_commit(self, message: str) -> vcs.GitResult
def git_pull(self) -> vcs.GitResult
def git_push(self, *, skip_check: bool = False) -> vcs.GitResult
def git_summary_line(self) -> str          # text for the strip
def audit_blocks_push(self) -> list[check_mod.Finding]   # errors only
```

Cache the status with an explicit `refresh=True` to re-read, so the strip does
not shell out to git on every repaint.

#### 4.2 The strip

A new `widgets.GitStrip`, packed under the toolbar (beside/above the existing
stale-tables banner — reuse the `after=self.toolbar` anchoring already in
`app.py`).

Content, left to right:

```
main · b6c9373 · ↑2 ↓0 · 3 changed · fetched 4 min ago        [Sync…]
```

* the state word drives emphasis; use the theme palette from Phase 2 rather
  than hardcoded colours;
* `fetched N ago` matters — ahead/behind is meaningless until a fetch, and a
  stale count is worse than none. Show `never fetched` when unknown;
* degrade gracefully: if the directory is not a git repo, show nothing at all
  rather than an error.

Refresh on: startup, after any applied plan, after a sync action, on `F5`, and
on an optional slow timer (default **off**; if added, local reads only, never
an automatic `fetch`).

#### 4.3 The sync view

A `SyncDialog` (`scripts/src/gui/sync_dialog.py`), sized and remembered via
Phase 1. Sections per §3.2/§3.4 of the discussion:

| Section | Content |
|---|---|
| Upstream | remote name, URL, tracking branch |
| HEAD | short + full sha, subject, author, relative date |
| Incoming | `git log HEAD..@{u}` — what a pull will bring |
| Outgoing | `git log @{u}..HEAD` — what a push will send |
| Working tree | grouped status list (staged / modified / deleted / untracked / unmerged) |
| Commit | message box prefilled from `suggest_commit_message`, with the file list |
| Actions | Fetch · Pull · Commit · Push · Commit & Push |
| Log | raw git stdout/stderr, monospace, copyable |

Requirements:

* **Disabled actions state their reason** next to the button, straight from
  `blockers()`. Never a silently greyed button.
* Every git call runs on a worker thread with the queue/`after` polling
  pattern already used in `import_dialog.py`. The UI must stay responsive and
  a second action must not start while one is running.
* **Pre-push audit:** run `check` first. If there are errors, list them and
  require an explicit `[ ] Push anyway (N errors)` checkbox before the Push
  button enables. Default unchecked; the choice is not remembered between
  openings.
* **After pull:** re-run `controller.refresh()` and the audit, and report if
  the pull arrived with stale tables or broken references. This is the main
  two-machine failure this whole feature is meant to catch.
* Confirm destructive-feeling actions in the plan-preview style already
  established, but do not invent an `ops.Plan`.

#### 4.4 Window title

Reflect state: `KiCad Custom Library — TPAxxxx · ↑2`. Keep it short; omit the
git part entirely when in sync or not a repo.

#### Acceptance checks — Phase 4

* Headless controller tests for `git_summary_line`, the caching behaviour of
  `git_status(refresh=…)`, and `audit_blocks_push`.
* Tk tests, using the temp-repo fixtures from Phase 3: the strip renders for
  each state; the sync dialog constructs; buttons are disabled with the right
  reason for a dirty tree, a diverged branch and no upstream; the push-anyway
  checkbox gates the Push button when the audit has errors.
* Assert the strip is absent (not erroring) when the library root is not a git
  repository.
* Manual: make a change on one machine, push from the GUI, pull on the other,
  confirm the audit runs on both sides and reports stale tables if present.

---

### Phase 5 — Polish

Small, independent items. Each is optional on its own; none should grow.

1. **Confirm on quit with uncommitted library changes.** Directly serves the
   multi-machine workflow; the classic failure is leaving machine A with
   unpushed parts. Offer *Commit & Push / Quit anyway / Cancel*.
2. **Empty-state guidance.** When the library or a category is empty, show a
   short instruction ("Click Import…, or drop a vendor ZIP in
   `staging-temp/intake/`") instead of a blank table.
3. **"Open in KiCad"** context action: `xdg-open` the `.kicad_sym` /
   `.kicad_mod`, which Plasma routes to the right editor. Best-effort —
   report failure in the status bar, do not raise. Use `start` on Windows and
   `open` on macOS, mirroring the existing `on_open_folder`.
4. **Keyboard:** `Ctrl+R` opens the sync view, `Ctrl+Q` quits. The existing
   `Ctrl+I`, `F2`, `Del`, `Ctrl+F`, `F5` stay.
5. **Persist the audit dialog's severity filter** within a session.
6. **Spacing pass.** Replace the ad-hoc paddings (`(10, 8)`, `(8, 4)`, `12`,
   `4`) with constants on an 8 px grid, defined once.

#### Acceptance checks — Phase 5

`pytest` green, `check` exits 0, and each item demonstrably works by hand. No
new dependencies.

---

## 5. Out of scope — do not do

* Part inventory, stock quantities, BOM reconciliation or purchasing. A
  separate project owns this.
* Porting to Qt/PySide. Keep `controller.py` toolkit-free so the option stays
  cheap, but do not take it.
* Any git operation that can lose work: force push, reset, stash, rebase,
  branch switching, conflict resolution. These stay in a terminal, and the UI
  should say so when it refuses.
* Changing the core library engine, the on-disk layout, or the plan/apply
  discipline for library files.
* Writing stock, inventory or GUI state into `.kicad_sym` / `.kicad_mod` /
  `provenance.json`.
* Adding runtime Python dependencies. `kdialog`/`zenity` are optional external
  binaries and must not be required.
* `git-lfs` migration. Worth considering eventually for `*.step`/`*.wrl`, but
  it is a separate decision with multi-machine consequences.

---

## 6. Working agreement

* One commit per phase, with a message explaining *why*, in the style of the
  existing history (see `git log`).
* Run `pytest -q` and `python scripts/lib_manager.py check` before every
  commit. Both must be clean.
* **Keep logic out of widgets.** If a decision can be made without Tk, it
  belongs in `controller.py` or `core/`, with a headless test. This is the
  single most important constraint in this plan.
* Tests that need a display must skip without one, and must keep skipping on
  Windows CI (`tests/test_gui_widgets.py` already has the markers and explains
  why).
* When unsure about Tk behaviour, check it against the running interpreter
  rather than guessing — §2 exists because several of these are
  counter-intuitive.
* After each phase, write a short summary: what changed, what tests were
  added, and any open question for the owner.
* Update `CHANGELOG.md` as you go; update `README.md` (GUI tour, the new sync
  view) and `AGENTS.md` (the new `core/vcs.py` module in the module map) in the
  final phase.
