"""
The import dialog.

Replaces the old flow, which was an `askopenfilename` -- a file chooser that
cannot select a folder -- followed by a free-text category box. Picking a lone
.kicad_mod there produced three empty directories and a manifest record with
no files.

Here the source can be any number of ZIPs, folders or individual files; every
candidate is listed with an editable name and its own category; and the exact
plan is shown before anything is written. The import runs on a worker thread
so a large archive does not freeze the window.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import Callable, Dict, List, Optional, Sequence

from ..core import ingest as ingest_mod
from ..core import naming
from ..core import ops
from .controller import Controller
from .widgets import (GAP, GAP_M, PAD_DIALOG, PAD_SECTION, CategoryPicker,
                      PlanPreview, modal)

COLUMNS = ("Include", "Kind", "Detected name", "New name", "Category", "Note")

try:  # optional drag-and-drop
    from tkinterdnd2 import DND_FILES  # type: ignore
    _HAS_DND = True
except Exception:  # noqa: BLE001 -- absence is normal, never fatal
    DND_FILES = None  # type: ignore
    _HAS_DND = False


class ImportDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, controller: Controller,
                 *, initial_category: str = "",
                 on_done: Optional[Callable[[str], None]] = None,
                 settings: Optional["st.Settings"] = None):
        super().__init__(parent)
        self.title("Import components")
        self.geometry("1040x620")
        self.minsize(760, 460)

        self.controller = controller
        self.on_done = on_done
        # Supplies and records the directory the dialogs open in. Optional so
        # the dialog still works standalone, e.g. in tests.
        self.settings = settings
        self.sources: List[Path] = []
        self.candidates: List[ingest_mod.Candidate] = []
        self._previews: List[ingest_mod.IngestPreview] = []
        self._row_category: Dict[str, str] = {}
        self._row_include: Dict[str, bool] = {}
        self._row_name: Dict[str, str] = {}
        self._editor: Optional[tk.Widget] = None
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None

        self._build(initial_category)
        modal(self, parent)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # -- construction -----------------------------------------------------
    def _build(self, initial_category: str) -> None:
        outer = ttk.Frame(self, padding=PAD_DIALOG)
        outer.pack(fill=tk.BOTH, expand=True)

        # Sources
        sources = ttk.LabelFrame(outer, text="Source", padding=PAD_SECTION)
        sources.pack(fill=tk.X)
        ttk.Button(sources, text="Add ZIP(s)...", command=self._add_zips).pack(side=tk.LEFT)
        ttk.Button(sources, text="Add folder...", command=self._add_folder).pack(
            side=tk.LEFT, padx=GAP)
        ttk.Button(sources, text="Add file(s)...", command=self._add_files).pack(side=tk.LEFT)
        ttk.Button(sources, text="Clear", command=self._clear_sources).pack(
            side=tk.LEFT, padx=GAP)
        self._source_label = ttk.Label(sources, text="No source selected")
        self._source_label.pack(side=tk.LEFT, padx=GAP_M)

        if _HAS_DND and hasattr(self, "drop_target_register"):
            try:
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", self._on_drop)
                ttk.Label(sources, text="(or drop files here)").pack(side=tk.RIGHT)
            except Exception:  # noqa: BLE001
                pass

        # Default category
        default = ttk.Frame(outer, padding=(0, GAP))
        default.pack(fill=tk.X)
        ttk.Label(default, text="Default category:").pack(side=tk.LEFT, padx=(0, GAP))
        self.category_picker = CategoryPicker(
            default, self.controller.categories(),
            validate=self.controller.validate_new_category,
            initial=initial_category,
            on_change=self._apply_default_category,
        )
        self.category_picker.pack(side=tk.LEFT)

        # Candidate table
        table_frame = ttk.LabelFrame(outer, text="Items to import", padding=PAD_SECTION)
        table_frame.pack(fill=tk.BOTH, expand=True)

        toolbar = ttk.Frame(table_frame)
        toolbar.pack(fill=tk.X, pady=(0, GAP))
        ttk.Button(toolbar, text="Toggle selected (Space)",
                   command=self._toggle_selected).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Apply category to selected",
                   command=self._apply_category_to_selected).pack(side=tk.LEFT, padx=GAP)
        ttk.Label(toolbar,
                  text="Double-click a New name or Category cell to edit it").pack(
            side=tk.RIGHT)

        tree_holder = ttk.Frame(table_frame)
        tree_holder.pack(fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(tree_holder, orient=tk.VERTICAL)
        self.tree = ttk.Treeview(
            tree_holder, columns=COLUMNS, show="headings",
            yscrollcommand=scroll.set, selectmode="extended",
        )
        scroll.config(command=self.tree.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for name, width in zip(COLUMNS, (70, 90, 230, 230, 180, 240)):
            self.tree.heading(name, text=name)
            self.tree.column(name, width=width, stretch=(name == "Note"))
        self.tree.bind("<Double-1>", self._begin_edit)
        self.tree.bind("<space>", lambda _e: self._toggle_selected())

        # Log and progress
        log_frame = ttk.LabelFrame(outer, text="Log", padding=PAD_SECTION)
        log_frame.pack(fill=tk.BOTH, expand=False, pady=(GAP, 0))
        self.progress = ttk.Progressbar(log_frame, mode="determinate")
        self.progress.pack(fill=tk.X, pady=(0, GAP))
        self.log = tk.Text(log_frame, height=6, wrap=tk.WORD,
                           font=tkfont.nametofont("TkFixedFont"))
        self.log.pack(fill=tk.BOTH, expand=True)
        self.log.config(state=tk.DISABLED)

        # Actions
        actions = ttk.Frame(outer)
        actions.pack(fill=tk.X, pady=(GAP_M, 0))
        ttk.Label(actions, text="If it already exists:").pack(side=tk.LEFT)
        self.conflict = tk.StringVar(value=ops.ConflictPolicy.SKIP.value)
        ttk.Combobox(actions, textvariable=self.conflict, state="readonly", width=12,
                     values=[p.value for p in ops.ConflictPolicy]).pack(
            side=tk.LEFT, padx=(0, GAP))

        self.import_button = ttk.Button(actions, text="Import",
                                        command=self._do_import, state=tk.DISABLED)
        self.import_button.pack(side=tk.RIGHT)
        self.preview_button = ttk.Button(actions, text="Preview changes",
                                         command=self._do_preview, state=tk.DISABLED)
        self.preview_button.pack(side=tk.RIGHT, padx=(0, GAP))
        ttk.Button(actions, text="Close", command=self._close).pack(
            side=tk.RIGHT, padx=(0, GAP))

    # -- sources ----------------------------------------------------------
    def _start_dir(self) -> Path:
        default = self.controller.default_dir(st.DIR_IMPORT_SOURCE)
        if self.settings is None:
            return default
        return self.settings.dir_for(st.DIR_IMPORT_SOURCE, default)

    def _remember_dir(self, path: Path) -> None:
        if self.settings is not None:
            self.settings.remember_dir(st.DIR_IMPORT_SOURCE, path)
            self.settings.save()

    def _add_zips(self) -> None:
        paths = filepicker.open_files(
            self, title="Select part archives", initialdir=self._start_dir(),
            filters=filepicker.ARCHIVE_FILTERS,
        )
        self._add_sources(paths)

    def _add_folder(self) -> None:
        """A real directory chooser -- the old dialog could not select one."""
        path = filepicker.open_directory(
            self, title="Select a part folder", initialdir=self._start_dir()
        )
        if path:
            self._add_sources([path])

    def _add_files(self) -> None:
        paths = filepicker.open_files(
            self, title="Select KiCad files", initialdir=self._start_dir(),
            filters=filepicker.KICAD_FILTERS,
        )
        self._add_sources(paths)

    def _on_drop(self, event) -> None:
        try:
            items = self.tk.splitlist(event.data)
        except tk.TclError:
            return
        self._add_sources([Path(i) for i in items])

    def _add_sources(self, paths: Sequence[Path]) -> None:
        added = [p for p in paths if p.exists() and p not in self.sources]
        if not added:
            return
        self.sources.extend(added)
        self._remember_dir(added[-1])
        self._refresh_sources_label()
        self._detect()

    def _clear_sources(self) -> None:
        self._release_previews()
        self.sources.clear()
        self.candidates.clear()
        self.tree.delete(*self.tree.get_children(""))
        self._refresh_sources_label()
        self._update_buttons()

    def _refresh_sources_label(self) -> None:
        if not self.sources:
            self._source_label.config(text="No source selected")
        elif len(self.sources) == 1:
            self._source_label.config(text=self.sources[0].name)
        else:
            self._source_label.config(text=f"{len(self.sources)} sources")

    # -- detection --------------------------------------------------------
    def _detect(self) -> None:
        """
        Find candidates in every source.

        Detection is read-only and quick, so it runs inline; only the import
        itself needs a thread.
        """
        self._release_previews()
        self.candidates = []
        self._row_category.clear()
        self._row_include.clear()
        self._row_name.clear()
        warnings: List[str] = []
        default_category = self.category_picker.get()

        for source in self.sources:
            try:
                bundle = ingest_mod.open_bundle(source)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{source.name}: {exc}")
                continue
            self._bundles_keep(bundle)
            found = ingest_mod.detect(bundle)
            warnings.extend(bundle.warnings)
            if not found:
                warnings.append(f"{source.name}: nothing importable found")
            for candidate in found:
                self.candidates.append(candidate)
                self._row_category[candidate.key] = default_category
                self._row_include[candidate.key] = True
                self._row_name[candidate.key] = candidate.target_name

        self._repopulate()
        self._log_lines(warnings)
        self._update_buttons()

    def _bundles_keep(self, bundle: ingest_mod.Bundle) -> None:
        """Keep bundles alive: the plan's copy operations read from them."""
        self._previews.append(
            ingest_mod.IngestPreview(
                bundle=bundle, candidates=[], pairing=ingest_mod.Pairing(),
                plan=ops.Plan(root=self.controller.root),
            )
        )

    def _release_previews(self) -> None:
        for preview in self._previews:
            preview.close()
        self._previews.clear()

    def _repopulate(self) -> None:
        self.tree.delete(*self.tree.get_children(""))
        for candidate in self.candidates:
            self.tree.insert(
                "", tk.END, iid=candidate.key, values=self._values_for(candidate)
            )

    def _values_for(self, candidate: ingest_mod.Candidate) -> tuple:
        return (
            "yes" if self._row_include[candidate.key] else "no",
            candidate.kind,
            candidate.detected_name,
            self._row_name[candidate.key],
            self._row_category[candidate.key] or "(choose one)",
            "; ".join(candidate.notes),
        )

    def _update_row(self, key: str) -> None:
        candidate = next(c for c in self.candidates if c.key == key)
        self.tree.item(key, values=self._values_for(candidate))

    def _update_buttons(self) -> None:
        ready = bool(self.candidates) and any(self._row_include.values())
        state = tk.NORMAL if ready else tk.DISABLED
        self.preview_button.config(state=state)
        self.import_button.config(state=state)

    # -- editing ----------------------------------------------------------
    def _apply_default_category(self, name: str) -> None:
        for key, value in list(self._row_category.items()):
            if not value:
                self._row_category[key] = name
                self._update_row(key)

    def _toggle_selected(self) -> None:
        for key in self.tree.selection():
            self._row_include[key] = not self._row_include[key]
            self._update_row(key)
        self._update_buttons()

    def _apply_category_to_selected(self) -> None:
        name = self.category_picker.get()
        if not name:
            messagebox.showinfo("Pick a category",
                                "Choose a default category first.", parent=self)
            return
        for key in self.tree.selection():
            self._row_category[key] = name
            self._update_row(key)

    def _begin_edit(self, event) -> None:
        if self._editor is not None:
            self._editor.destroy()
            self._editor = None
        row_id = self.tree.identify_row(event.y)
        column_id = self.tree.identify_column(event.x)
        if not row_id or not column_id:
            return
        index = int(column_id[1:]) - 1
        if COLUMNS[index] not in ("New name", "Category"):
            return

        x, y, width, height = self.tree.bbox(row_id, column_id)
        if COLUMNS[index] == "New name":
            var = tk.StringVar(value=self._row_name[row_id])
            entry = ttk.Entry(self.tree, textvariable=var)
            entry.place(x=x, y=y, width=width, height=height)
            entry.focus_set()
            entry.selection_range(0, tk.END)

            def commit(*_a) -> None:
                value = naming.sanitize(var.get().strip()) if var.get().strip() else ""
                if value:
                    self._row_name[row_id] = value
                    self._update_row(row_id)
                entry.destroy()
                self._editor = None

            entry.bind("<Return>", commit)
            entry.bind("<FocusOut>", commit)
            entry.bind("<Escape>", lambda _e: (entry.destroy(),
                                               setattr(self, "_editor", None)))
            self._editor = entry
        else:
            var = tk.StringVar(value=self._row_category[row_id])
            combo = ttk.Combobox(self.tree, textvariable=var, state="readonly",
                                 values=self.controller.categories())
            combo.place(x=x, y=y, width=width, height=height)
            combo.focus_set()

            def chosen(*_a) -> None:
                self._row_category[row_id] = var.get()
                self._update_row(row_id)
                combo.destroy()
                self._editor = None

            combo.bind("<<ComboboxSelected>>", chosen)
            combo.bind("<Escape>", lambda _e: (combo.destroy(),
                                               setattr(self, "_editor", None)))
            self._editor = combo

    # -- planning and importing -------------------------------------------
    def _grouped_plan(self) -> Optional[ops.Plan]:
        """
        One plan covering every selected item, grouped by target category.

        Built here rather than per-source so the preview is a single list of
        what will happen.
        """
        selected = [c for c in self.candidates if self._row_include[c.key]]
        if not selected:
            return None

        missing = [c for c in selected if not self._row_category[c.key]]
        if missing:
            messagebox.showerror(
                "Category required",
                f"{len(missing)} item(s) have no category. Choose a default "
                f"category, or set one per row.",
                parent=self,
            )
            return None

        combined = ops.Plan(root=self.controller.root, title="Import components")
        policy = ops.ConflictPolicy(self.conflict.get())

        by_category: Dict[str, List[ingest_mod.Candidate]] = {}
        for candidate in selected:
            candidate.target_name = self._row_name[candidate.key]
            by_category.setdefault(self._row_category[candidate.key], []).append(candidate)

        for category, items in sorted(by_category.items()):
            try:
                plan = ingest_mod.plan_ingest(
                    self.controller.root, category, items,
                    conflict=policy, prov=self.controller.prov,
                )
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Cannot plan import", str(exc), parent=self)
                return None
            combined.extend(plan)
        return combined

    def _do_preview(self) -> None:
        plan = self._grouped_plan()
        if plan is None:
            return
        PlanPreview(self, plan, apply_label="Close").wait_window()

    def _do_import(self) -> None:
        plan = self._grouped_plan()
        if plan is None:
            return
        if not PlanPreview.confirm(self, plan, apply_label="Import"):
            return

        self.import_button.config(state=tk.DISABLED)
        self.preview_button.config(state=tk.DISABLED)
        self.progress.config(maximum=max(1, len(plan.operations)), value=0)
        self._log_lines([f"Importing {len(plan.operations)} operation(s)..."])

        def work() -> None:
            try:
                result = self.controller.apply(
                    plan, on_progress=lambda op: self._queue.put(("progress", op))
                )
                self._queue.put(("done", result))
            except Exception as exc:  # noqa: BLE001 -- surfaced on the UI thread
                self._queue.put(("error", exc))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()
        self.after(50, self._drain_queue)

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "progress":
                    self.progress.step(1)
                    self._log_lines([payload.describe(self.controller.root)])
                elif kind == "done":
                    self._finish(payload)
                    return
                else:
                    self._log_lines([f"ERROR: {payload}"])
                    messagebox.showerror("Import failed", str(payload), parent=self)
                    self.import_button.config(state=tk.NORMAL)
                    self.preview_button.config(state=tk.NORMAL)
                    return
        except queue.Empty:
            pass
        self.after(50, self._drain_queue)

    def _finish(self, result: ops.Result) -> None:
        self._log_lines([result.summary(self.controller.root)])
        if not result.ok:
            messagebox.showerror(
                "Import incomplete",
                result.summary(self.controller.root), parent=self,
            )
        if self.on_done:
            self.on_done(result.summary(self.controller.root).splitlines()[0])
        self._detect()  # re-detect so conflicts now show against the new state

    def _log_lines(self, lines: Sequence[str]) -> None:
        if not lines:
            return
        self.log.config(state=tk.NORMAL)
        for line in lines:
            self.log.insert(tk.END, line.rstrip() + "\n")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _close(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            if not messagebox.askyesno(
                "Import running",
                "An import is still running. Close anyway?", parent=self,
            ):
                return
        self._release_previews()
        self.destroy()
