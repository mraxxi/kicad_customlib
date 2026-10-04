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
    ):
        super().__init__(parent)
        self.controller = controller
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
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        # Categories
        left = ttk.Frame(paned, padding=4)
        paned.add(left, weight=1)
        ttk.Label(left, text="Categories").pack(anchor=tk.W, pady=(0, 4))
        self.category_list = tk.Listbox(left, exportselection=False, activestyle="none")
        self.category_list.pack(fill=tk.BOTH, expand=True)
        self.category_list.bind("<<ListboxSelect>>", lambda _e: self.refresh_items())
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
                            command=self.refresh_items).pack(side=tk.LEFT)
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
        self.tree.set_columns(COLUMNS[kind])
        self.tree.tag_configure("problem", font=self._underlined())
        self._rows = self.controller.rows(
            kind, category=self.selected_category(), search=self.search.get()
        )
        self.tree.repopulate(self._rows)
        self._selection_changed()

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
