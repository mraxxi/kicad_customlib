"""
The library browser: a category list and a table of items.

Everything shown comes from the Controller as Rows, and every Treeview item
carries an explicit string iid, so nothing is ever recovered by reading
display text back out of the widget -- which is how the old GUI turned the
category '3255' into the integer 3255 and then failed to find it.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional

from .controller import COLUMNS, KIND_FOOTPRINT, KIND_MODEL, KIND_SYMBOL, Controller, Row
from .widgets import SortableTree

ALL_CATEGORIES = "[All categories]"

# First-run column widths, chosen for the content each column holds. Saved
# widths override these.
DEFAULT_WIDTHS = {
    KIND_SYMBOL: {"Name": 240, "Category": 190, "Footprint": 280,
                  "3D": 48, "Source": 200},
    KIND_FOOTPRINT: {"Name": 280, "Category": 190, "3D models": 90,
                     "Used by": 80, "Source": 200},
    KIND_MODEL: {"Name": 280, "Category": 190, "Used by": 80,
                 "Size": 80, "Source": 200},
}


class LibraryBrowser(ttk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        controller: Controller,
        *,
        on_select: Optional[Callable[[Optional[Row]], None]] = None,
        on_activate: Optional[Callable[[Row], None]] = None,
        on_category_menu: Optional[Callable[[str, int, int], None]] = None,
        on_item_menu: Optional[Callable[[int, int], None]] = None,
        layout: Optional["LayoutStore"] = None,
    ):
        super().__init__(parent)
        self.controller = controller
        # Supplies and receives remembered sash/column/view state. Optional so
        # the browser still works standalone, e.g. in tests.
        self.layout = layout
        self.on_select = on_select
        self.on_activate = on_activate
        self.on_category_menu = on_category_menu
        self.on_item_menu = on_item_menu

        self.kind = tk.StringVar(value=KIND_SYMBOL)
        self.search = tk.StringVar(value="")
        self._rows: List[Row] = []

        self._build()
        self.refresh()

    def _build(self) -> None:
        self.paned = paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)
        # sashpos() is clamped to zero before the pane has been mapped and
        # sized, so the restore has to wait for <Map>.
        paned.bind("<Map>", self._restore_sash, add="+")
        paned.bind("<ButtonRelease-1>", self._remember_sash, add="+")

        # Categories
        left = ttk.Frame(paned, padding=4)
        paned.add(left, weight=1)
        ttk.Label(left, text="Categories").pack(anchor=tk.W, pady=(0, 4))
        self.category_list = tk.Listbox(left, exportselection=False, activestyle="none")
        self.category_list.pack(fill=tk.BOTH, expand=True)
        self.category_list.bind("<<ListboxSelect>>", self._on_category_chosen)
        self.category_list.bind("<Button-3>", self._category_context)

        # Items
        right = ttk.Frame(paned, padding=4)
        paned.add(right, weight=3)

        controls = ttk.Frame(right)
        controls.pack(fill=tk.X, pady=(0, 6))
        for label, value in (("Symbols", KIND_SYMBOL),
                             ("Footprints", KIND_FOOTPRINT),
                             ("3D models", KIND_MODEL)):
            ttk.Radiobutton(controls, text=label, value=value, variable=self.kind,
                            command=self._on_kind_chosen).pack(side=tk.LEFT)
        ttk.Label(controls, text="Search:").pack(side=tk.LEFT, padx=(14, 4))
        self.search_entry = ttk.Entry(controls, textvariable=self.search, width=28)
        self.search_entry.pack(side=tk.LEFT)
        self.search.trace_add("write", lambda *_a: self.refresh_items())
        ttk.Button(controls, text="Clear",
                   command=lambda: self.search.set("")).pack(side=tk.LEFT, padx=4)

        holder = ttk.Frame(right)
        holder.pack(fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(holder, orient=tk.VERTICAL)
        self.tree = SortableTree(holder, COLUMNS[KIND_SYMBOL],
                                 yscrollcommand=scroll.set, selectmode="extended")
        scroll.config(command=self.tree.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # A row with a problem is marked by an underline rather than a colour,
        # so it reads the same in a light or a dark theme.
        self.tree.tag_configure("problem", font=self._underlined())
        self.tree.bind("<ButtonRelease-1>", self._remember_columns, add="+")
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        self.tree.bind("<Double-1>", self._activate)
        self.tree.bind("<Button-3>", self._item_context)

    @staticmethod
    def _underlined():
        from tkinter import font as tkfont
        f = tkfont.nametofont("TkDefaultFont").copy()
        f.configure(underline=True)
        return f

    # -- data -------------------------------------------------------------
    def restore_view(self) -> None:
        """
        Re-select the kind and category the user was last looking at.

        Called once after construction rather than from __init__, so a caller
        that does not persist layout is unaffected.
        """
        if not self.layout:
            return
        kind, category = self.layout.view()
        if kind in COLUMNS:
            self.kind.set(kind)
        if category and category in self.controller.categories():
            names = self.controller.categories()
            self.category_list.selection_clear(0, tk.END)
            self.category_list.selection_set(names.index(category) + 1)
        self.refresh_items()

    def refresh(self) -> None:
        """Reload categories and items, preserving the current selection."""
        previous = self.selected_category()
        self.category_list.delete(0, tk.END)
        self.category_list.insert(tk.END, ALL_CATEGORIES)
        names = self.controller.categories()
        for name in names:
            self.category_list.insert(tk.END, self.controller.category_label(name))

        index = 0
        if previous and previous in names:
            index = names.index(previous) + 1
        self.category_list.selection_clear(0, tk.END)
        self.category_list.selection_set(index)
        self.refresh_items()

    def refresh_items(self) -> None:
        kind = self.kind.get()
        widths = self.layout.columns(kind) if self.layout else {}
        self.tree.set_columns(COLUMNS[kind], widths=widths,
                              defaults=DEFAULT_WIDTHS.get(kind))
        self.tree.tag_configure("problem", font=self._underlined())
        if self.layout:
            column, reverse = self.layout.sort()
            self.tree.apply_sort(column, reverse)
        # The view is deliberately NOT remembered here. refresh_items() runs
        # during construction, with defaults in place, so writing from it
        # overwrote the saved view before restore_view() could read it.
        # Remembering happens only on an explicit user action.
        self._rows = self.controller.rows(
            kind, category=self.selected_category(), search=self.search.get()
        )
        self.tree.repopulate(self._rows)
        self._selection_changed()

    # -- user actions that change the view ---------------------------------
    def _on_kind_chosen(self) -> None:
        self.refresh_items()
        self._remember_view()

    def _on_category_chosen(self, _event=None) -> None:
        self.refresh_items()
        self._remember_view()

    def _remember_view(self) -> None:
        if self.layout:
            self.layout.remember_view(self.kind.get(), self.selected_category())

    # -- layout persistence ------------------------------------------------
    def _restore_sash(self, _event=None) -> None:
        if not self.layout:
            return
        position = self.layout.sash()
        if not position:
            return
        self.update_idletasks()
        try:
            if 0 < position < self.paned.winfo_width():
                self.paned.sashpos(0, position)
        except tk.TclError:
            pass

    def _remember_sash(self, _event=None) -> None:
        if not self.layout:
            return
        try:
            self.layout.remember_sash(self.paned.sashpos(0))
        except tk.TclError:
            pass

    def _remember_columns(self, _event=None) -> None:
        if self.layout:
            self.layout.remember_columns(self.kind.get(), self.tree.column_widths())

    def remember_sort(self) -> None:
        if self.layout:
            column, reverse = self.tree.sort_state
            if column:
                self.layout.remember_sort(column, reverse)

    def selected_category(self) -> Optional[str]:
        selection = self.category_list.curselection()
        if not selection or selection[0] == 0:
            return None
        names = self.controller.categories()
        index = selection[0] - 1
        return names[index] if 0 <= index < len(names) else None

    def selected_rows(self) -> List[Row]:
        by_iid = {r.iid: r for r in self._rows}
        return [by_iid[iid] for iid in self.tree.selection() if iid in by_iid]

    def selected_row(self) -> Optional[Row]:
        rows = self.selected_rows()
        return rows[0] if len(rows) == 1 else None

    def focus_search(self) -> None:
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, tk.END)

    # -- events -----------------------------------------------------------
    def _selection_changed(self, _event=None) -> None:
        if self.on_select:
            self.on_select(self.selected_row())

    def _activate(self, _event=None) -> None:
        row = self.selected_row()
        if row and self.on_activate:
            self.on_activate(row)

    def _item_context(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        if self.on_item_menu:
            self.on_item_menu(event.x_root, event.y_root)

    def _category_context(self, event) -> None:
        index = self.category_list.nearest(event.y)
        if index <= 0:
            return
        self.category_list.selection_clear(0, tk.END)
        self.category_list.selection_set(index)
        self.refresh_items()
        category = self.selected_category()
        if category and self.on_category_menu:
            self.on_category_menu(category, event.x_root, event.y_root)

    def select_item(self, kind: str, category: str, name: str) -> bool:
        """Jump to an item, e.g. from a double-clicked audit finding."""
        self.kind.set(kind)
        self.category_list.selection_clear(0, tk.END)
        self.category_list.selection_set(0)
        self.search.set("")
        self.refresh_items()
        from .controller import make_iid
        iid = make_iid(kind, category, name)
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)
            return True
        return False
