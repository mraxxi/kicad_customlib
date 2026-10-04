# Changelog

All notable changes to this library's tooling. The library *content* is
tracked by git history, not here.

## [Unreleased]

### GUI plan Phase 3 — `core/vcs.py`, the git layer
- New `core/vcs.py`: a toolkit-free git layer, so the CLI and the GUI share
  one set of rules and the whole of it is testable headlessly.
- **It can only do four things, and none of them can lose work.** The module
  issues `status`, `rev-parse`, `symbolic-ref`, `log`, `rev-list`, `remote`,
  `config`, `add`, `commit`, `fetch`, `pull --ff-only` and `push`. There is no
  force push, reset, checkout, stash, rebase, explicit merge, clean or gc
  anywhere in it, because on a library synced between two machines the work
  those would discard exists in no other clone. A test reads the subcommands
  straight off the source and fails on anything outside the allowlist.
- `RepoStatus.state` reduces everything to one word, in a fixed precedence:
  `not_a_repo` → `operation_in_progress` → `unmerged` → `detached` →
  `no_upstream` → `diverged` → `behind` → `ahead` → `dirty` → `in_sync`.
  Several of those are true at once in practice, so the order is asserted
  directly rather than inferred from a repository that happens to be in one
  of them.
- `blockers()` returns action → reason, and the reasons are whole sentences
  because the interface shows them verbatim. A greyed-out button that does not
  say why is the thing this avoids. A dirty tree blocks a pull deliberately:
  git's own message talks about overwriting local changes, which does not tell
  you what to do, whereas "commit them first" does.
- Hardening: `GIT_TERMINAL_PROMPT=0`, empty `GIT_ASKPASS`/`SSH_ASKPASS`,
  `ssh -oBatchMode=yes` and no `DISPLAY`, so a remote needing a password
  cannot turn a call into a hang waiting on a terminal the GUI does not have;
  `GIT_OPTIONAL_LOCKS=0` so reading status never takes the index lock out from
  under a terminal; `LC_ALL=C` for stable parsing. Local reads time out at
  10 s and transfers at 120 s, and a timeout comes back as a result, never an
  exception.
- Status is parsed from `--porcelain=v1 -z`. The default format quotes and
  escapes any path with a space or a non-ASCII byte, and this library has
  plenty of both; `-z` gives the literal path, at the cost of a rename
  emitting two fields for one entry.
- `fetch`'s age is part of the indicator, not decoration: ahead/behind is
  computed against the remote-tracking ref, so "in sync" after a week without
  a fetch means nothing. The status line says `never fetched` when it is
  unknown.
- `suggest_commit_message()` reads the diff and says what changed — `Add 3
  symbols to Conn_XT`, `Regenerate master library tables`, `Remove 1
  footprint from C` — with a bulleted body for a mixed change. On a repository
  synced between machines the history is the only record of when a part
  arrived, and "Update files" destroys that. Always editable.
- `CommitPreview` rather than an `ops.Plan`. A Plan exists because KiCad
  S-expressions are fragile and interlinked; a commit is one invocation over a
  list of paths, and dressing it up as a Plan would only obscure that. The
  commit stages exactly the paths the preview listed, so nothing created while
  a dialog sat open can slip in.
- New CLI: `sync status [--json]`, `sync fetch`, `sync pull`, `sync commit
  [-m]`, `sync push [--skip-check]`, following the existing confirm/`--yes`/
  `--dry-run` conventions. `pull` re-runs the audit on what arrived, which is
  the main two-machine failure this is meant to catch; `push` audits first and
  `--skip-check` overrides it, because a one-person library needs to be able
  to park a knowingly-broken state on the remote.
- `tests/test_vcs.py` (93 tests) drives a temp repo plus a `git init --bare`
  remote and a second clone, so every state — including behind, diverged and a
  real conflicted merge — is reachable with no network and no display.

### GUI plan Phase 2 — File dialogs and appearance
- New `gui/filepicker.py`. On Linux `tkinter.filedialog` is not native — Tk
  draws its own Motif-era widget — so the picker now prefers `kdialog` (the
  real Plasma dialog), then `zenity`, then Tk. Both are optional external
  binaries, not Python dependencies. A helper that fails is remembered and not
  retried for the rest of the session, cancel (exit 1) is distinguished from
  failure, and the Tk window keeps repainting while a helper is open.
  `KICAD_CUSTOMLIB_FILEPICKER` forces a backend. Windows and macOS keep Tk,
  where it already is the platform dialog.
- Dialogs open somewhere sensible. No `initialdir` was passed anywhere before,
  so they landed wherever the process happened to be started. Import now
  starts at `staging-temp/intake/`, everything else at the library root, and
  the last-used directory is remembered per purpose.
- New `gui/theme.py`. No ttk theme was ever selected, so Linux got Tk's dated
  `default`; it now picks `clam` on Linux and `vista` on Windows. Where KDE's
  scheme can be read from `kdeglobals`, the window, view, selection,
  alternate-row and negative colours are fed to ttk, giving correct light/dark
  and the user's accent with no dependency. Parsing tolerates the repeated
  keys and `[Colors:Header][Inactive]`-style sections that defeat a strict INI
  parser, and every step degrades to the plain theme.
- The `ttk.Combobox` popup is a bare `tk.Listbox` outside the ttk theme, so it
  is styled through the option database instead, and given room for 18 rows
  rather than three.
- Treeview rows have a comfortable height derived from the font, and alternate
  using the desktop's own alternate-row colour. A row with a dangling
  reference is coloured with the scheme's negative colour, keeping the
  underline as the theme-independent fallback.
- The category picker's `＋ New category...` entry is gone, replaced by a
  `New…` button. Creating a library is a deliberate act and should not be
  reachable by mis-clicking in a list of existing ones.
- Windows HiDPI: `SetProcessDpiAwareness` is called before the Tk root exists,
  without which Tk renders blurry.

### GUI plan Phase 1 — Layout persistence
- New `gui/settings.py` replaces the two ad-hoc config helpers in `app.py`.
  Window size and position, the browser sash, per-kind column widths, the
  selected kind and category, and the sort column and direction all persist,
  keyed per screen configuration (`3840x1080@96`) so attaching a monitor gets
  its own profile. The search box is deliberately not persisted.
- Saves are debounced 800 ms after the last `<Configure>` or sash drag, plus an
  unconditional save on close.
- Preferences fail soft — a damaged or future-schema file falls back to
  defaults rather than blocking startup. The opposite of `provenance.json` on
  purpose, and the module explains why.
- A withdrawn Tk root reports `1x1+0+0`; geometry is only stored from a
  mapped, normal-state window at or above its minimum size.
- The pre-schema config (a bare `{"last_category": ...}`) is migrated rather
  than discarded on a version check.
- Two of my own read paths wrote, the same mistake Phase 0 fixed in the core:
  reading a geometry profile created it, and `refresh_items()` remembered the
  view during construction and so overwrote what `restore_view()` was about to
  read. Reading no longer mutates.
- `SortableTree` takes remembered widths and stretches only its last column;
  with stretch everywhere Tk overrides restored widths on every resize.
- **Library → Reset window layout** clears every profile, keeping remembered
  categories and folders. Ctrl+Q quits.

### GUI plan Phase 0 — Plan purity
- `refactor.plan_*` and `ingest.plan_ingest` mutated the `Provenance` object
  while building the plan, breaking the rule that planning touches nothing.
  The GUI rebuilds the plan on every keystroke for its live preview, so a
  rename typed character by character walked the provenance key through every
  partial name and stranded it on the first — losing the original vendor
  filename and import date. Plans now carry `ops.ProvenanceEdit` records, and
  `provenance.apply_edits()` runs them once, after the operations succeed. The
  `prov=` parameter is gone from those functions so the mistake is
  unrepresentable.
- `tests/test_plan_purity.py` asserts the provenance object is byte-identical
  after planning every operation. The old test compared file bytes on disk and
  never passed a provenance object, which is why it saw nothing.
- New `lib_manager.py prune-provenance`. Rather than discarding stale entries,
  it re-homes those whose kind and name still match exactly one item on disk —
  only the category was wrong — and prints the details of anything it cannot
  place before dropping it. Ambiguous matches are refused. Applied to this
  library: 9 entries recovered, 6 dropped, 15 audit warnings down to 0.
- `check`'s `provenance-stale` remedy claimed the entry "will be dropped on
  the next write", which was false — nothing ever pruned them.

### Fixes found by the first CI runs
- `test_case_colliding_symbols_in_one_category_are_an_error` could only pass on
  a case-sensitive filesystem: it wrote two files differing only by case, which
  Windows and macOS collapse into one, leaving nothing to detect. It now skips
  where case is folded (probed, not inferred from `sys.platform`), and a new
  test drives the same detection through a hand-built index so it is verified
  on every platform.
- Tcl on the hosted Windows image fails after roughly 34 Tk interpreter
  create/destroy cycles. A shared session-scoped root was tried as a
  workaround and hung every macOS job indefinitely, so per-test roots — the
  configuration known to pass on Linux and macOS — are kept, and the Tk widget
  tests skip on Windows *CI only*. A developer on a real Windows desktop still
  runs them. In CI the module is exercised by macOS alone, since the Ubuntu
  runner has no display and skips it on that basis; Linux coverage comes from
  running the suite locally.
- The stale-tables banner is anchored to an explicit toolbar reference instead
  of `winfo_children()[0]`. The old form was correct in practice, since the
  toolbar is the first widget created with the root as parent, but it tied the
  layout to creation order needlessly. `widgets.StaleBanner` carried the same
  assumption and was unused; removed.
- CI jobs carry `timeout-minutes`, so a deadlock fails in 15 minutes instead of
  running against the six-hour default.

### Phase 4 — Documentation and repository hygiene
- `README.md` rewritten for the current architecture: setup on a new machine,
  the everyday workflows, the GUI tour, the full CLI reference, how provenance
  works, and what to do after pulling on the other machine.
- `AGENTS.md` rewritten around the two rules that explain the rest — the disk
  is the source of truth, and plan before apply — with a module map, the
  procedure for adding a new operation, and the measured KiCad 10 facts kept
  as §7.
- Every reference to `manifest.json` removed, and the TPA3155/TPA3255 example
  mismatch corrected.
- GitHub Actions CI on Linux, Windows and macOS × Python 3.11/3.13: runs the
  test suite, then `lib_manager.py check` so a broken library fails the build.
  A separate job rejects CRLF in KiCad text sources.
- `requirements-dev.txt` for the dev-only `pytest` dependency.
- **Regenerated the master tables.** Both committed tables listed `3255` and
  `TI-TPAxxx_AUDIO-AMP`, neither of which contains a single file; KiCad showed
  two empty libraries, and because git does not track an empty directory the
  other machine's tables pointed at folders that did not exist there. The two
  empty category directories are left in place pending a decision on them —
  `check` reports them as warnings.

### Phase 3 — GUI
- Rewritten over the plan/apply core, split into `controller.py` (a view-model
  with no Tk, so it is tested headlessly), `widgets.py`, `browser.py`,
  `import_dialog.py`, `rename_dialog.py` and `app.py`.
- Import accepts any number of ZIPs, folders or individual files, with a real
  directory chooser. The old flow used `askopenfilename`, which cannot select a
  folder; picking a lone `.kicad_mod` there created three empty directories and
  a manifest record with no files.
- Every tree item carries an explicit string `iid`, so nothing is recovered by
  reading display text back out of a widget — which is what turned the category
  `3255` into the integer `3255` and broke the lookup, and what made parsing a
  category out of `Name (3)` with `split(" (")` fragile.
- Categories are picked from a list, with a deliberate "New category" step that
  validates as you type, instead of being retyped as free text in every dialog.
- Nothing is applied without showing the plan, including deletes, which state
  which symbols, footprints or derived symbols they will leave dangling.
- Imports run on a worker thread with progress and a log; one failed item no
  longer aborts the batch.
- Successes go to a status bar rather than a message box. A banner appears when
  the tables go stale. Tk named fonts and no hard-coded backgrounds, so dark
  themes work; HiDPI scaling; keyboard shortcuts.
- Preferences moved to `~/.config/kicad_customlib/gui.json`, outside the repo.
- `core/manifest.py` and the deprecated path-based `s_expr` helpers deleted,
  now that nothing uses them.

### Phase 2 — CLI
- Every mutating command builds a plan, prints it, and asks before writing.
  `--dry-run` stops at the plan, `--yes` skips the question, and a
  non-interactive run without `--yes` is refused so a script cannot mutate the
  library by accident.
- Commands: `list`, `check`, `generate`, `ingest`, `sync-staging`, `rename`,
  `move`, `package`, `migrate-manifest`, `gui`. `rename` and `move` accept
  `Category:Name` or a bare name with `--category`.
- `--conflict skip|overwrite|rename` wherever a target can already exist.
- `sync-staging` handles folders and ZIPs, keeps going after a failure, counts
  an item that yielded nothing as failed rather than silently successful, and
  `--archive` moves processed items to `staging-temp/imported_archive/`.
- `check` exits non-zero on errors and `--json` makes it machine-readable.
- The `ManifestManager` that was constructed before every command is gone, so a
  corrupt manifest no longer breaks unrelated commands.
- Errors print one clear line and suggest `--debug`; a missing `tkinter`
  explains how to install it per platform.

### Phase 1 — Core rewrite
**`s_expr.py`** — text-in/text-out and edit-span based. No `re.sub` with an
interpolated replacement: a value containing a backslash used to be
reinterpreted as a group reference and corrupt the file. Added quote- and
paren-aware navigation, `top_level_symbols`, `get_property`/`set_property`
scoped to one symbol block (the old code patched the first `Footprint` in the
file and mislabelled every other symbol in a bundle), `rename_symbol` covering
unit sub-symbols and `Value`, `retarget_extends` for the cross-file derived
case, `all_models` returning every `(model …)` block with offsets parsed,
`set_model_path` that replaces only the path token, `add_model`, and
`extract_symbol_file` for splitting multi-symbol libraries. Reads tolerate BOM
and CRLF; writes are atomic and always LF; read-then-write is byte-identical.

**`naming.py`** — validation against the character set measured from the
official libraries, `:` and path separators and Windows reserved names
rejected, case-insensitive duplicate detection, official-nickname collision
warnings read from the installed KiCad. `sanitize()` turns
`Inductor_2*10uH_Leaded_7W15` into `Inductor_2_10uH_Leaded_7W15` — the
spelling that bundle's own `Footprint` property already used.

**`library.py`** — read-only scanner. Derives every relationship from the
files, models the reality that symbols may share a footprint and a footprint
may have zero or many models, and resolves dangling references, non-portable
model paths, parentless derived symbols and orphans. A malformed file becomes
a recorded problem instead of aborting the scan.

**`ops.py`** — the plan/apply engine. Building a plan touches nothing.
Conflicts default to skip-with-warning. Directories are created lazily by the
file operations themselves, which is what stops empty categories appearing. A
mid-plan failure reports exactly what was applied, and no file is left
half-written.

**`provenance.py`** — replaces `manifest.json` with the metadata the disk
cannot provide: original name, source, import date. Keyed
`<Category>/<kind>/<name>`, so the same name in two categories no longer
overwrites itself. No global `last_updated` (it made every operation a git
conflict) and no history array. Corruption raises with the parse position and
leaves the file untouched, where the old loader swallowed JSON errors and let
the next save wipe the data. `migrate-manifest` drops records nothing on disk
backs up.

**`ingest.py`** — finds every candidate instead of taking `[0]` of each list,
so a multi-part archive no longer loses all but the first part. Rejects unsafe
archive members, skips `__MACOSX` resource forks and `.DS_Store`, imports a
lone `.kicad_mod` as a footprint, splits multi-symbol libraries one file per
symbol, names each 3D model after its **footprint**, and reports what it could
not pair rather than guessing.

**`refactor.py`** — rename and move with library-wide reference rewriting,
including `(extends …)` across sibling files in a symdir. Refuses to move a
symbol that is the parent of derived siblings. Accumulates edits per file so a
refactor touching one file twice keeps both changes.

**`table_gen.py`** — only libraries that contain a file are listed, plus
`is_stale()` and `diff_tables()`.

**`check.py`** — audits from the disk rather than from the manifest, with
severities, remedies and a non-zero exit on errors. The old `check` reported
"Library is 100% healthy, 1 valid part" for a library containing no files at
all. `kicad-cli` validation follows the measured asymmetry: `fp export svg`
for footprints, `sym upgrade` on a throwaway copy for symbols.

**`packager.py`** — resolves models by reading the footprint's own `(model …)`
path instead of globbing `<footprint>*.step`, which found nothing whenever the
model was named after the symbol. Writes project tables at the project root
where KiCad reads them, merges instead of replacing, backs up first, computes
`${KIPRJMOD}` paths from the real `--out`, refuses an `--out` outside the
project, packages a derived symbol's parent, and reports unresolved items.

### Phase 0 — Safety net and verified KiCad facts
- `.gitattributes` forcing LF for KiCad text sources and marking STEP/WRL
  binary, so multi-megabyte CRLF vendor models are never renormalized.
- `tests/` with a pytest harness and fixture builders for KiCad 10 files and
  the vendor ZIP shapes (SnapEDA, Ultra Librarian, Component Search Engine,
  macOS junk, multi-part, multi-symbol cache, zip-slip), verified against the
  real `kicad-cli`.
- `AGENTS.md` §7: facts measured across all 22 784 official symbol files.
  Two of them corrected the work plan:
  - `(extends …)` is **always** cross-file. Of 12 249 derived symbols, none
    define their parent in the same file and none point outside their own
    symdir. Renaming a symbol must rewrite siblings.
  - `kicad-cli` validation is asymmetric: `fp export svg` returns 2 on a
    corrupt footprint, but `sym export svg` returns 0 and silently writes
    nothing.
  Also recorded: exactly one top-level symbol per file (22 784/22 784); the
  single case-only name exception (`tusb564.kicad_sym` → `TUSB564`), so that
  check must be case-insensitive; the official filename character set; and
  that `MCU_RaspberryPi` — an `AGENTS.md` "Good" example — collides with a real
  official library.
