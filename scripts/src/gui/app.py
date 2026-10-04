"""
The application shell.

A thin layer over the Controller: menus and buttons gather intent, the
Controller turns it into an ops.Plan, the user sees the plan, and only then
is anything written.

Routine outcomes go to the status bar rather than a message box, so a normal
import no longer requires dismissing a dialog. Message boxes are kept for
errors and for confirmations.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import Optional

from ..core import check as check_mod
from ..core import ops
from .browser import LibraryBrowser
from .controller import Controller, Row, parse_iid
from .import_dialog import ImportDialog
from .rename_dialog import CategoryRenameDialog, MoveDialog, RenameDialog
from .widgets import PlanPreview, StatusBar, apply_scaling, modal

CONFIG_DIR = Path.home() / ".config" / "kicad_customlib"
CONFIG_FILE = CONFIG_DIR / "gui.json"


def _load_config() -> dict:
    """GUI preferences live outside the repo so they are never committed."""
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_config(data: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # a preference failing to save is not worth interrupting the user


class LibraryManagerApp:
    def __init__(self, root: tk.Tk, lib_root: Path):
        self.root = root
        self.controller = Controller(lib_root)
        self.config = _load_config()

        root.title(f"KiCad Custom Library Manager - {self.controller.root.name}")
        root.geometry("1180x720")
        root.minsize(900, 560)
        apply_scaling(root)

        self._build()
        self.refresh()

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        # Kept as an attribute because the stale-tables banner packs itself
        # directly after it. Anchoring on winfo_children()[0] instead happens
        # to work -- this is the first widget created with the root as parent --
        # but it ties the layout to creation order for no reason.
        self.toolbar = ttk.Frame(self.root, padding=(10, 8))
        toolbar = self.toolbar
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text="KiCad Custom Library Manager",
                  font=self._bold()).pack(side=tk.LEFT)
        ttk.Label(toolbar, text=str(self.controller.root)).pack(side=tk.LEFT, padx=12)

        ttk.Button(toolbar, text="Package project...",
                   command=self.on_package).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text="Audit",
                   command=self.on_audit).pack(side=tk.RIGHT, padx=6)
        ttk.Button(toolbar, text="Process staging",
                   command=self.on_staging).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text="Import...",
                   command=self.on_import).pack(side=tk.RIGHT, padx=6)

        # The stale-tables banner sits between the toolbar and the browser.
        self.banner = ttk.Frame(self.root, padding=(10, 6))
        ttk.Label(self.banner,
                  text="The master library tables are out of date.",
                  font=self._bold()).pack(side=tk.LEFT)
        ttk.Button(self.banner, text="Regenerate now",
                   command=self.on_generate).pack(side=tk.RIGHT)

        content = ttk.Frame(self.root, padding=(10, 0))
        content.pack(fill=tk.BOTH, expand=True)

        # The details pane is created before the browser: constructing the
        # browser immediately fires a selection callback, which writes here.
        details = ttk.LabelFrame(content, text="Details", padding=8)
        self.details = tk.Text(details, height=7, wrap=tk.WORD,
                               font=tkfont.nametofont("TkFixedFont"))
        self.details.pack(fill=tk.X)
        self.details.config(state=tk.DISABLED)

        self.browser = LibraryBrowser(
            content, self.controller,
            on_select=self.on_row_selected,
            on_activate=lambda row: self.on_rename(),
            on_item_menu=self._show_item_menu,
            on_category_menu=self._show_category_menu,
        )
        self.browser.pack(fill=tk.BOTH, expand=True)
        details.pack(fill=tk.X, pady=(8, 8))

        self.status = StatusBar(self.root)
        self.status.pack(fill=tk.X)

        self._build_menus()
        self._bind_keys()

    @staticmethod
    def _bold() -> tkfont.Font:
        f = tkfont.nametofont("TkDefaultFont").copy()
        f.configure(weight="bold")
        return f

    def _build_menus(self) -> None:
        self.item_menu = tk.Menu(self.root, tearoff=0)
        self.item_menu.add_command(label="Rename...", command=self.on_rename)
        self.item_menu.add_command(label="Move to category...", command=self.on_move)
        self.item_menu.add_separator()
        self.item_menu.add_command(label="Copy name", command=self.on_copy_name)
        self.item_menu.add_command(label="Open containing folder",
                                   command=self.on_open_folder)
        self.item_menu.add_separator()
        self.item_menu.add_command(label="Delete...", command=self.on_delete)

        self.category_menu = tk.Menu(self.root, tearoff=0)
        self.category_menu.add_command(label="Rename category...",
                                       command=self.on_rename_category)
        self.category_menu.add_command(label="Import into this category...",
                                       command=self.on_import_here)

        menubar = tk.Menu(self.root)
        library = tk.Menu(menubar, tearoff=0)
        library.add_command(label="Import...", accelerator="Ctrl+I",
                            command=self.on_import)
        library.add_command(label="Process staging folder",
                            command=self.on_staging)
        library.add_separator()
        library.add_command(label="Regenerate tables", command=self.on_generate)
        library.add_command(label="Audit", command=self.on_audit)
        library.add_separator()
        library.add_command(label="Package project...", command=self.on_package)
        library.add_separator()
        library.add_command(label="Quit", command=self.root.destroy)
        menubar.add_cascade(label="Library", menu=library)

        item = tk.Menu(menubar, tearoff=0)
        item.add_command(label="Rename...", accelerator="F2", command=self.on_rename)
        item.add_command(label="Move to category...", command=self.on_move)
        item.add_command(label="Delete...", accelerator="Del", command=self.on_delete)
        menubar.add_cascade(label="Item", menu=item)
        self.root.config(menu=menubar)

    def _bind_keys(self) -> None:
        self.root.bind("<Control-i>", lambda _e: self.on_import())
        self.root.bind("<F2>", lambda _e: self.on_rename())
        self.root.bind("<Delete>", lambda _e: self.on_delete())
        self.root.bind("<Control-f>", lambda _e: self.browser.focus_search())
        self.root.bind("<F5>", lambda _e: self.refresh())

    # -- state ------------------------------------------------------------
    def refresh(self) -> None:
        self.controller.refresh()
        self.browser.refresh()
        self.status.set_counts(self.controller.counts_summary())
        if self.controller.tables_are_stale:
            if not self.banner.winfo_ismapped():
                self.banner.pack(fill=tk.X, after=self.toolbar)
        else:
            self.banner.pack_forget()
        if self.controller.provenance_error:
            self.status.set(f"provenance.json problem: "
                            f"{self.controller.provenance_error}")

    def on_row_selected(self, row: Optional[Row]) -> None:
        self.details.config(state=tk.NORMAL)
        self.details.delete("1.0", tk.END)
        if row is not None:
            self.details.insert("1.0", self.controller.details(row))
        self.details.config(state=tk.DISABLED)

    def _require_row(self) -> Optional[Row]:
        row = self.browser.selected_row()
        if row is None:
            self.status.set("Select a single item first.")
        return row

    def _show_item_menu(self, x: int, y: int) -> None:
        if self.browser.selected_rows():
            self.item_menu.tk_popup(x, y)

    def _show_category_menu(self, category: str, x: int, y: int) -> None:
        self._menu_category = category
        self.category_menu.tk_popup(x, y)

    # -- actions ----------------------------------------------------------
    def on_generate(self) -> None:
        plan = self.controller.plan_generate()
        if plan.is_empty:
            self.status.set("Tables are already up to date.")
            return
        if not PlanPreview.confirm(self.root, plan, apply_label="Regenerate"):
            return
        result = self.controller.apply(plan)
        self.refresh()
        self.status.set("Regenerated the master tables." if result.ok
                        else "Could not regenerate the tables.")

    def on_import(self, category: str = "") -> None:
        remembered = category or self.config.get("last_category", "")
        dialog = ImportDialog(
            self.root, self.controller,
            initial_category=remembered,
            on_done=self._after_import,
        )
        self.root.wait_window(dialog)
        chosen = dialog.category_picker.get()
        if chosen:
            self.config["last_category"] = chosen
            _save_config(self.config)
        self.refresh()

    def on_import_here(self) -> None:
        self.on_import(getattr(self, "_menu_category", ""))

    def _after_import(self, message: str) -> None:
        self.refresh()
        self.status.set(message)

    def on_rename(self) -> None:
        row = self._require_row()
        if row is None:
            return
        dialog = RenameDialog(self.root, self.controller, row)
        self.root.wait_window(dialog)
        if dialog.applied:
            self.refresh()
            self.status.set(getattr(dialog, "result_message", "Renamed."))

    def on_move(self) -> None:
        row = self._require_row()
        if row is None:
            return
        dialog = MoveDialog(self.root, self.controller, row)
        self.root.wait_window(dialog)
        if dialog.applied:
            self.refresh()
            self.status.set(getattr(dialog, "result_message", "Moved."))

    def on_rename_category(self) -> None:
        category = getattr(self, "_menu_category", "")
        if not category:
            return
        dialog = CategoryRenameDialog(self.root, self.controller, category)
        self.root.wait_window(dialog)
        if dialog.applied:
            self.refresh()
            self.status.set(getattr(dialog, "result_message", "Category renamed."))

    def on_delete(self) -> None:
        rows = self.browser.selected_rows()
        if not rows:
            self.status.set("Select something to delete first.")
            return
        plan = self.controller.plan_delete(rows)
        if not PlanPreview.confirm(self.root, plan, apply_label="Delete"):
            return
        result = self.controller.apply(plan)
        self.refresh()
        self.status.set(f"Deleted {len(result.applied)} file(s)." if result.ok
                        else "Delete failed; see the details above.")

    def on_copy_name(self) -> None:
        rows = self.browser.selected_rows()
        if not rows:
            return
        text = "\n".join(
            f"{r.category}:{r.name}" if r.kind != "model" else r.name for r in rows
        )
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status.set(f"Copied {len(rows)} name(s) to the clipboard.")

    def on_open_folder(self) -> None:
        row = self._require_row()
        if row is None or row.path is None:
            return
        folder = row.path.parent
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            elif sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as exc:
            messagebox.showerror("Could not open the folder", str(exc),
                                 parent=self.root)
            return
        self.status.set(f"Opened {folder}")

    def on_staging(self) -> None:
        intake = self.controller.root / "staging-temp" / "intake"
        if not intake.is_dir():
            messagebox.showinfo(
                "Nothing staged",
                f"Create {intake} and drop <Category>/<part> folders or ZIPs "
                f"inside, then try again.",
                parent=self.root,
            )
            return
        jobs = [
            (cat.name, item)
            for cat in sorted(p for p in intake.iterdir() if p.is_dir())
            for item in sorted(cat.iterdir())
            if item.is_dir() or item.suffix.lower() == ".zip"
        ]
        if not jobs:
            messagebox.showinfo("Nothing staged", f"{intake} is empty.",
                                parent=self.root)
            return

        combined = ops.Plan(root=self.controller.root,
                            title=f"Import {len(jobs)} staged item(s)")
        previews = []
        try:
            for category, item in jobs:
                try:
                    preview = self.controller.prepare_import(item, category)
                except Exception as exc:  # noqa: BLE001 -- one item must not stop the rest
                    combined.warn(f"{item.name}: {exc}")
                    continue
                previews.append(preview)
                combined.extend(preview.plan)

            if not PlanPreview.confirm(self.root, combined, apply_label="Import all"):
                return
            result = self.controller.apply(combined)
        finally:
            for preview in previews:
                preview.close()

        self.refresh()
        self.status.set(result.summary(self.controller.root).splitlines()[0])

    def on_audit(self) -> None:
        AuditDialog(self.root, self.controller, self.browser)

    def on_package(self) -> None:
        project = filedialog.askdirectory(
            parent=self.root, title="Select the KiCad project to package")
        if not project:
            return
        try:
            plan, result = self.controller.plan_package(Path(project))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Cannot package", str(exc), parent=self.root)
            return

        if not PlanPreview.confirm(
            self.root, plan, apply_label="Package",
        ):
            return
        applied = ops.apply(plan)
        messagebox.showinfo(
            "Packaged" if applied.ok else "Packaging incomplete",
            result.summary() + "\n\n" + applied.summary(self.controller.root),
            parent=self.root,
        )
        self.status.set(f"Packaged {len(result.packaged_symbols)} symbol(s) and "
                        f"{len(result.packaged_footprints)} footprint(s).")


class AuditDialog(tk.Toplevel):
    """A filterable, copyable list of findings; double-click jumps to the item."""

    def __init__(self, parent: tk.Misc, controller: Controller,
                 browser: LibraryBrowser):
        super().__init__(parent)
        self.title("Library audit")
        self.geometry("1000x560")
        self.controller = controller
        self.browser = browser

        body = ttk.Frame(self, padding=10)
        body.pack(fill=tk.BOTH, expand=True)

        controls = ttk.Frame(body)
        controls.pack(fill=tk.X, pady=(0, 6))
        self.severity = tk.StringVar(value="all")
        for label, value in (("All", "all"), ("Errors", check_mod.ERROR),
                             ("Warnings", check_mod.WARNING), ("Notes", check_mod.INFO)):
            ttk.Radiobutton(controls, text=label, value=value,
                            variable=self.severity,
                            command=self._repopulate).pack(side=tk.LEFT)
        self.summary_label = ttk.Label(controls, text="Running...")
        self.summary_label.pack(side=tk.RIGHT)

        holder = ttk.Frame(body)
        holder.pack(fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(holder, orient=tk.VERTICAL)
        self.tree = ttk.Treeview(
            holder, columns=("Severity", "Code", "Where", "Message"),
            show="headings", yscrollcommand=scroll.set,
        )
        scroll.config(command=self.tree.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for name, width in (("Severity", 90), ("Code", 220), ("Where", 240),
                            ("Message", 420)):
            self.tree.heading(name, text=name)
            self.tree.column(name, width=width, stretch=(name == "Message"))
        self.tree.bind("<Double-1>", self._jump)
        self.tree.bind("<<TreeviewSelect>>", self._show_remedy)

        self.remedy = ttk.Label(body, text="", wraplength=940, justify=tk.LEFT)
        self.remedy.pack(fill=tk.X, pady=(6, 0))

        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(buttons, text="Re-run", command=self._run).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Copy all",
                   command=self._copy_all).pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side=tk.RIGHT)

        self.report: Optional[check_mod.Report] = None
        modal(self, parent)
        self.after(10, self._run)

    def _run(self) -> None:
        self.summary_label.config(text="Running...")
        self.update_idletasks()
        self.report = self.controller.audit()
        counts = self.report.counts()
        self.summary_label.config(
            text=f"{counts[check_mod.ERROR]} error(s), "
                 f"{counts[check_mod.WARNING]} warning(s), "
                 f"{counts[check_mod.INFO]} note(s)"
            + ("" if self.report.kicad_cli_used else "  (kicad-cli not found)")
        )
        self._repopulate()

    def _repopulate(self) -> None:
        self.tree.delete(*self.tree.get_children(""))
        if self.report is None:
            return
        wanted = self.severity.get()
        for index, finding in enumerate(self.report.sorted()):
            if wanted != "all" and finding.severity != wanted:
                continue
            self.tree.insert("", tk.END, iid=str(index),
                             values=(finding.severity, finding.code,
                                     finding.where, finding.message))
        self._findings = {
            str(i): f for i, f in enumerate(self.report.sorted())
        }

    def _selected_finding(self):
        selection = self.tree.selection()
        if not selection:
            return None
        return self._findings.get(selection[0])

    def _show_remedy(self, _event=None) -> None:
        finding = self._selected_finding()
        self.remedy.config(text=f"Suggested fix: {finding.remedy}"
                           if finding and finding.remedy else "")

    def _jump(self, _event=None) -> None:
        finding = self._selected_finding()
        if finding is None or ":" not in finding.where:
            return
        category, name = finding.where.split(":", 1)
        for kind in ("symbol", "footprint", "model"):
            if self.browser.select_item(kind, category, name):
                self.destroy()
                return

    def _copy_all(self) -> None:
        if self.report is None:
            return
        self.clipboard_clear()
        self.clipboard_append(self.report.summary())


def launch_gui(lib_root: Path) -> None:
    try:
        from tkinterdnd2 import TkinterDnD  # type: ignore
        root = TkinterDnD.Tk()
    except Exception:  # noqa: BLE001 -- drag-and-drop is optional
        root = tk.Tk()
    LibraryManagerApp(root, Path(lib_root))
    root.mainloop()
