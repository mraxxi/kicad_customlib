"""
Desktop GUI interface for KICAD_CUSTOM_LIB Manager.
Built with Python's native Tkinter (zero external dependencies).
"""

import sys
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
from typing import Optional

from ..core.manifest import ManifestManager
from ..core.table_gen import generate_tables
from ..core.packager import ProjectPackager


class LibraryManagerApp:
    def __init__(self, root: tk.Tk, lib_root: Path):
        self.root = root
        self.lib_root = lib_root.resolve()
        self.manifest = ManifestManager(self.lib_root)
        self.packager = ProjectPackager(self.lib_root)

        self.root.title("KiCad Custom Library Manager")
        self.root.geometry("980x640")
        self.root.minsize(800, 500)

        # Style configuration
        self.style = ttk.Style()
        self.style.theme_use("clam")

        self._build_ui()
        self.refresh_data()

    def _build_ui(self):
        # Top Header Frame
        header = ttk.Frame(self.root, padding="10")
        header.pack(fill=tk.X)

        title_lbl = ttk.Label(
            header,
            text="KiCad Custom Library Manager",
            font=("Helvetica", 14, "bold")
        )
        title_lbl.pack(side=tk.LEFT)

        path_lbl = ttk.Label(
            header,
            text=f"Path: {self.lib_root.name}",
            font=("Helvetica", 9),
            foreground="#666666"
        )
        path_lbl.pack(side=tk.LEFT, padx=15)

        gen_btn = ttk.Button(
            header,
            text="Regenerate Tables",
            command=self.on_generate_tables
        )
        gen_btn.pack(side=tk.RIGHT)

        health_btn = ttk.Button(
            header,
            text="Audit Health",
            command=self.on_audit_health
        )
        health_btn.pack(side=tk.RIGHT, padx=5)

        # Main Paned Window (Left Categories, Right Parts)
        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Left Frame: Category List
        left_frame = ttk.Frame(paned, padding="5")
        paned.add(left_frame, weight=1)

        cat_lbl = ttk.Label(left_frame, text="Categories", font=("Helvetica", 10, "bold"))
        cat_lbl.pack(anchor=tk.W, pady=(0, 5))

        self.cat_listbox = tk.Listbox(
            left_frame,
            selectmode=tk.SINGLE,
            font=("Monospace", 10),
            exportselection=False
        )
        self.cat_listbox.pack(fill=tk.BOTH, expand=True)
        self.cat_listbox.bind("<<ListboxSelect>>", self.on_category_select)

        # Right Frame: Parts Table & Details
        right_frame = ttk.Frame(paned, padding="5")
        paned.add(right_frame, weight=3)

        parts_lbl = ttk.Label(right_frame, text="Components", font=("Helvetica", 10, "bold"))
        parts_lbl.pack(anchor=tk.W, pady=(0, 5))

        # Parts Treeview Table
        cols = ("Part Name", "Category", "Symbol", "Footprint", "3D Model")
        self.parts_tree = ttk.Treeview(right_frame, columns=cols, show="headings", height=10)
        for c in cols:
            self.parts_tree.heading(c, text=c)
            self.parts_tree.column(c, width=120)
        self.parts_tree.column("Part Name", width=160)
        self.parts_tree.column("Category", width=180)
        self.parts_tree.pack(fill=tk.BOTH, expand=True)
        self.parts_tree.bind("<<TreeviewSelect>>", self.on_part_select)

        # Details Panel
        self.details_frame = ttk.LabelFrame(right_frame, text="Part Details", padding="8")
        self.details_frame.pack(fill=tk.X, pady=(8, 0))

        self.details_txt = tk.Text(
            self.details_frame,
            height=5,
            state=tk.DISABLED,
            font=("Monospace", 9),
            bg="#f8f9fa"
        )
        self.details_txt.pack(fill=tk.X)

        # Bottom Action Bar
        action_bar = ttk.Frame(self.root, padding="10")
        action_bar.pack(fill=tk.X)

        add_btn = ttk.Button(
            action_bar,
            text="+ Ingest Part / ZIP",
            command=self.on_ingest_dialog
        )
        add_btn.pack(side=tk.LEFT)

        move_btn = ttk.Button(
            action_bar,
            text="Move / Reorganize...",
            command=self.on_move_dialog
        )
        move_btn.pack(side=tk.LEFT, padx=5)

        sync_staging_btn = ttk.Button(
            action_bar,
            text="Process staging-temp/",
            command=self.on_sync_staging
        )
        sync_staging_btn.pack(side=tk.LEFT, padx=5)

        pkg_btn = ttk.Button(
            action_bar,
            text="Package for Project (Export)",
            command=self.on_package_project_dialog
        )
        pkg_btn.pack(side=tk.RIGHT)

    def refresh_data(self):
        """Reloads manifest and repopulates lists."""
        self.manifest = ManifestManager(self.lib_root)
        categories = self.manifest.list_categories()

        self.cat_listbox.delete(0, tk.END)
        self.cat_listbox.insert(tk.END, "[All Categories]")
        for c in categories:
            count = len(self.manifest.list_parts(c))
            self.cat_listbox.insert(tk.END, f"{c} ({count})")

        self.cat_listbox.select_set(0)
        self.populate_parts_table(None)

    def populate_parts_table(self, category: Optional[str]):
        """Fills parts treeview with components."""
        for item in self.parts_tree.get_children():
            self.parts_tree.delete(item)

        parts = self.manifest.list_parts(category)
        for part_name, record in parts.items():
            cat = record.get("category", "-")
            files = record.get("files", {})
            sym_ok = "✓" if "symbol" in files and (self.lib_root / files["symbol"]).exists() else "✗"
            fp_ok = "✓" if "footprint" in files and (self.lib_root / files["footprint"]).exists() else "✗"
            step_ok = "✓" if "model_3d" in files and (self.lib_root / files["model_3d"]).exists() else "✗"

            self.parts_tree.insert(
                "",
                tk.END,
                values=(part_name, cat, sym_ok, fp_ok, step_ok),
                tags=(part_name,)
            )

    def on_category_select(self, event):
        sel = self.cat_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx == 0:
            self.populate_parts_table(None)
        else:
            cat_text = self.cat_listbox.get(idx)
            cat_name = cat_text.split(" (")[0]
            self.populate_parts_table(cat_name)

    def on_part_select(self, event):
        sel = self.parts_tree.selection()
        if not sel:
            return
        item = self.parts_tree.item(sel[0])
        part_name = item["values"][0]
        record = self.manifest.get_part(part_name)
        if not record:
            return

        self.details_txt.config(state=tk.NORMAL)
        self.details_txt.delete("1.0", tk.END)

        files = record.get("files", {})
        info = (
            f"Part: {part_name}\n"
            f"Category: {record.get('category')}\n"
            f"Footprint ID: {record.get('footprint_identifier') or 'Not set'}\n"
            f"Import Source: {record.get('original_import_name')} ({record.get('import_date', '')[:10]})\n"
            f"Symbol File:    {files.get('symbol', 'None')}\n"
            f"Footprint File: {files.get('footprint', 'None')}\n"
            f"3D Model File:  {files.get('model_3d', 'None')}"
        )
        self.details_txt.insert(tk.END, info)
        self.details_txt.config(state=tk.DISABLED)

    def on_generate_tables(self):
        sym_p, fp_p = generate_tables(self.lib_root)
        messagebox.showinfo(
            "Success",
            f"Generated Master Tables:\n- {sym_p.name}\n- {fp_p.name}\n\nUsing ${{KICAD_CUSTOM_LIB}}."
        )

    def on_audit_health(self):
        report = self.manifest.audit_health()
        if report["healthy"]:
            messagebox.showinfo("Health Audit", f"All {report['total_parts']} parts are healthy! Zero issues found.")
        else:
            issue_str = "\n".join([f"• [{i['part']}] {i['detail']}" for i in report["issues"][:10]])
            if len(report["issues"]) > 10:
                issue_str += f"\n...and {len(report['issues']) - 10} more."
            messagebox.showwarning("Health Audit Issues Found", issue_str)

    def on_ingest_dialog(self):
        file_path = filedialog.askopenfilename(
            title="Select Component ZIP or Folder",
            filetypes=[("Archives / KiCad", "*.zip *.kicad_sym *.kicad_mod *.step"), ("All Files", "*.*")]
        )
        if not file_path:
            return

        # Category input dialog
        dialog = tk.Toplevel(self.root)
        dialog.title("Ingest Component")
        dialog.geometry("400x180")
        dialog.transient(self.root)

        ttk.Label(dialog, text="Target Category (e.g. TI-TPAxxx_AUDIO-AMP):").pack(pady=(15, 5))
        cat_entry = ttk.Entry(dialog, width=35)
        cat_entry.pack(pady=5)
        cat_entry.focus()

        def do_ingest():
            cat = cat_entry.get().strip()
            if not cat:
                messagebox.showerror("Error", "Category name cannot be empty")
                return
            try:
                record = self.manifest.ingest_part(Path(file_path), cat)
                dialog.destroy()
                self.refresh_data()
                messagebox.showinfo("Success", f"Ingested '{record['display_name']}' into category '{cat}'")
            except Exception as e:
                messagebox.showerror("Ingest Failed", str(e))

        ttk.Button(dialog, text="Ingest", command=do_ingest).pack(pady=15)

    def on_move_dialog(self):
        sel = self.parts_tree.selection()
        if not sel:
            messagebox.showwarning("Selection Required", "Please select a part to move.")
            return

        part_name = self.parts_tree.item(sel[0])["values"][0]

        dialog = tk.Toplevel(self.root)
        dialog.title(f"Move {part_name}")
        dialog.geometry("400x180")
        dialog.transient(self.root)

        ttk.Label(dialog, text=f"Move '{part_name}' to Category:").pack(pady=(15, 5))
        cat_entry = ttk.Entry(dialog, width=35)
        cat_entry.pack(pady=5)
        cat_entry.focus()

        def do_move():
            new_cat = cat_entry.get().strip()
            if not new_cat:
                messagebox.showerror("Error", "Category name cannot be empty")
                return
            ok = self.manifest.move_part(part_name, new_cat)
            if ok:
                dialog.destroy()
                self.refresh_data()
                messagebox.showinfo("Success", f"Moved '{part_name}' to category '{new_cat}'")
            else:
                messagebox.showerror("Error", "Failed to move part.")

        ttk.Button(dialog, text="Move Part", command=do_move).pack(pady=15)

    def on_sync_staging(self):
        intake_dir = self.lib_root / "staging-temp" / "intake"
        if not intake_dir.exists() or not any(intake_dir.iterdir()):
            messagebox.showinfo("Staging Empty", f"No items found in {intake_dir}.\nDrop category folders with parts there to batch-sync.")
            return

        count = 0
        for cat_dir in intake_dir.iterdir():
            if cat_dir.is_dir():
                cat_name = cat_dir.name
                for part_dir in cat_dir.iterdir():
                    if part_dir.is_dir():
                        self.manifest.ingest_part(part_dir, cat_name)
                        count += 1
        self.refresh_data()
        messagebox.showinfo("Staging Processed", f"Successfully ingested {count} parts from staging.")

    def on_package_project_dialog(self):
        proj_dir = filedialog.askdirectory(title="Select KiCad Project Directory to Package")
        if not proj_dir:
            return

        try:
            res = self.packager.package_for_project(Path(proj_dir))
            messagebox.showinfo(
                "Project Packaged",
                f"Exported {len(res['packaged_symbols'])} symbols and {len(res['packaged_footprints'])} footprints to:\n{res['output_directory']}\n\nProject is now 100% self-contained!"
            )
        except Exception as e:
            messagebox.showerror("Packaging Error", str(e))


def launch_gui(lib_root: Path):
    root = tk.Tk()
    app = LibraryManagerApp(root, lib_root)
    root.mainloop()
