"""
Reusable widgets.

No business logic here: everything these need to decide comes from the
Controller. Fonts are the Tk named fonts so the interface follows the desktop
theme, and no background colours are hard-coded, so dark themes are not
turned into unreadable light-grey panels.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..core import ops
from . import theme

NEW_CATEGORY_BUTTON = "New…"


def apply_scaling(root: tk.Misc) -> None:
    """Make the interface usable on a HiDPI display."""
    try:
        dpi = root.winfo_fpixels("1i")
    except tk.TclError:
        return
    if dpi > 120:
        root.tk.call("tk", "scaling", max(1.0, dpi / 72.0))


def modal(window: tk.Toplevel, parent: tk.Misc) -> None:
    """Standard modal setup, including Escape to close."""
    window.transient(parent.winfo_toplevel())
    window.bind("<Escape>", lambda _e: window.destroy())
    window.update_idletasks()
    try:
        window.grab_set()
    except tk.TclError:
        pass  # another grab is active; not worth failing over


class StatusBar(ttk.Frame):
    """
    A one-line status area.

    Replaces the success message boxes the old GUI threw up after every
    action, which forced a click to continue. Errors and confirmations still
    get a dialog; routine outcomes belong here.
    """

    def __init__(self, parent: tk.Misc):
        super().__init__(parent, padding=(8, 4))
        self._var = tk.StringVar(value="Ready")
        self._label = ttk.Label(self, textvariable=self._var, anchor=tk.W)
        self._label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._detail = tk.StringVar(value="")
        ttk.Label(self, textvariable=self._detail, anchor=tk.E).pack(side=tk.RIGHT)

    def set(self, message: str, *, detail: str = "") -> None:
        self._var.set(message)
        self._detail.set(detail)

    def set_counts(self, summary: str) -> None:
        self._detail.set(summary)


class CategoryPicker(ttk.Frame):
    """
    A read-only combobox of existing categories, plus an entry that creates
    one.

    The old GUI asked for the category as free text in every dialog, so a
    typo silently created a new library -- which is how a category called
    '3255' came to exist. Creating one is now a deliberate act with live
    validation, and the directories are still only created when a file is
    actually placed in them.
    """

    def __init__(
        self,
        parent: tk.Misc,
        categories: Sequence[str],
        *,
        validate: Callable[[str], tuple[bool, str]],
        initial: str = "",
        on_change: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(parent)
        self._validate = validate
        self._on_change = on_change
        self._categories = list(categories)
        self._value = tk.StringVar(value=initial)

        self._combo = ttk.Combobox(
            self, textvariable=self._value, state="readonly",
            values=self._choices(), width=34, height=18,
        )
        self._combo.pack(side=tk.LEFT)
        self._combo.bind("<<ComboboxSelected>>", self._on_selected)

        # A real button, rather than a magic entry in the dropdown list.
        # Creating a library is a deliberate act and should not be something
        # you can do by mis-clicking in a list of existing ones.
        self._new_button = ttk.Button(
            self, text=NEW_CATEGORY_BUTTON, width=7, command=self._prompt_new
        )
        self._new_button.pack(side=tk.LEFT, padx=(4, 0))

        self._hint = ttk.Label(self, text="", wraplength=360, justify=tk.LEFT)
        self._hint.pack(side=tk.LEFT, padx=(8, 0))

    def _choices(self) -> List[str]:
        return list(self._categories)

    def _on_selected(self, _event=None) -> None:
        self._refresh_hint()
        if self._on_change:
            self._on_change(self.get())

    def _refresh_hint(self) -> None:
        name = self.get()
        if not name:
            self._hint.config(text="")
            return
        ok, message = self._validate(name)
        self._hint.config(text=message if not ok else (message or ""))

    def _prompt_new(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("New category")
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text="Category name:").grid(row=0, column=0, sticky=tk.W)
        entry_var = tk.StringVar()
        entry = ttk.Entry(body, textvariable=entry_var, width=38)
        entry.grid(row=1, column=0, sticky=tk.EW, pady=(2, 6))
        entry.focus_set()

        feedback = ttk.Label(body, text="", wraplength=380, justify=tk.LEFT)
        feedback.grid(row=2, column=0, sticky=tk.W)

        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, sticky=tk.E, pady=(10, 0))
        create = ttk.Button(buttons, text="Create", state=tk.DISABLED)
        create.pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(
            side=tk.RIGHT, padx=(0, 6)
        )

        def revalidate(*_a) -> None:
            name = entry_var.get().strip()
            if not name:
                feedback.config(text="")
                create.config(state=tk.DISABLED)
                return
            ok, message = self._validate(name)
            feedback.config(text=message)
            create.config(state=tk.NORMAL if ok else tk.DISABLED)

        def commit() -> None:
            name = entry_var.get().strip()
            ok, _msg = self._validate(name)
            if not ok:
                return
            if name not in self._categories:
                self._categories.append(name)
                self._categories.sort(key=lambda n: (n.lower(), n))
                self._combo.config(values=self._choices())
            self._value.set(name)
            dialog.destroy()
            self._refresh_hint()
            if self._on_change:
                self._on_change(name)

        entry_var.trace_add("write", revalidate)
        create.config(command=commit)
        entry.bind("<Return>", lambda _e: commit())
        modal(dialog, self)

    def get(self) -> str:
        return self._value.get().strip()

    def set(self, name: str) -> None:
        self._value.set(name)
        self._refresh_hint()


class PlanPreview(tk.Toplevel):
    """
    Shows a plan and asks whether to run it.

    `result` is True only when the user pressed Apply, so a closed window is
    never taken as consent.
    """

    def __init__(self, parent: tk.Misc, plan: ops.Plan, *, apply_label: str = "Apply"):
        super().__init__(parent)
        self.title(plan.title or "Review changes")
        self.result = False
        self.geometry("820x520")
        self.minsize(560, 320)

        body = ttk.Frame(self, padding=10)
        body.pack(fill=tk.BOTH, expand=True)

        header = (
            f"{len(plan.operations)} operation(s), "
            f"{len(plan.warnings)} warning(s), {len(plan.conflicts)} conflict(s)"
        )
        ttk.Label(body, text=header).pack(anchor=tk.W, pady=(0, 6))

        text_frame = ttk.Frame(body)
        text_frame.pack(fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL)
        text = tk.Text(
            text_frame, wrap=tk.NONE, font=tkfont.nametofont("TkFixedFont"),
            yscrollcommand=scroll.set, height=18,
        )
        scroll.config(command=text.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        text.insert("1.0", plan.summary())
        text.config(state=tk.DISABLED)   # readable and copyable, not editable

        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(10, 0))
        apply_button = ttk.Button(buttons, text=apply_label, command=self._accept)
        apply_button.pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        if plan.is_empty:
            apply_button.config(state=tk.DISABLED)
        else:
            apply_button.focus_set()

        self.bind("<Return>", lambda _e: self._accept())
        modal(self, parent)

    def _accept(self) -> None:
        self.result = True
        self.destroy()

    @classmethod
    def confirm(cls, parent: tk.Misc, plan: ops.Plan, **kw) -> bool:
        dialog = cls(parent, plan, **kw)
        parent.wait_window(dialog)
        return dialog.result


class SortableTree(ttk.Treeview):
    """
    A Treeview whose columns sort on click and whose selection and scroll
    position survive a repopulate.
    """

    def __init__(self, parent: tk.Misc, columns: Sequence[str], **kw):
        super().__init__(parent, columns=list(columns), show="headings", **kw)
        self._sort_column: Optional[str] = None
        self._sort_reverse = False
        self.set_columns(columns)

    def set_columns(
        self,
        columns: Sequence[str],
        widths: Optional[Dict[str, int]] = None,
        defaults: Optional[Dict[str, int]] = None,
    ) -> None:
        """
        Install the columns, optionally at remembered widths.

        Only the last column stretches. With `stretch=True` everywhere, Tk
        re-apportions the columns whenever the window resizes and immediately
        overrides any width the user set or we restored -- so the trade is
        deliberate: columns no longer grow with the window, and the widths the
        user chose stick.
        """
        widths = widths or {}
        defaults = defaults or {}
        self.config(columns=list(columns))
        last = columns[-1] if columns else None
        for name in columns:
            self.heading(name, text=name,
                         command=lambda c=name: self._sort_by(c))
            width = widths.get(name) or defaults.get(name) or 150
            self.column(name, width=width, stretch=(name == last))

    def column_widths(self) -> Dict[str, int]:
        """The current width of each column, for persisting."""
        out: Dict[str, int] = {}
        for name in self.cget("columns"):
            try:
                out[str(name)] = int(self.column(str(name), "width"))
            except (tk.TclError, TypeError, ValueError):
                continue
        return out

    @property
    def sort_state(self) -> Tuple[Optional[str], bool]:
        return self._sort_column, self._sort_reverse

    def apply_sort(self, column: Optional[str], reverse: bool) -> None:
        """Restore a remembered sort without toggling it."""
        if not column or column not in self.cget("columns"):
            return
        self._sort_column = column
        self._sort_reverse = not reverse   # _sort_by flips it
        self._sort_by(column)

    def _sort_by(self, column: str) -> None:
        if self._sort_column == column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column, self._sort_reverse = column, False
        items = [(self.set(iid, column), iid) for iid in self.get_children("")]
        items.sort(key=lambda pair: pair[0].lower(), reverse=self._sort_reverse)
        for index, (_value, iid) in enumerate(items):
            self.move(iid, "", index)

    def configure_appearance(self) -> None:
        """
        Set up alternating row colours and the problem highlight.

        Striping uses the desktop's own alternate-row colour when it could be
        read, so it matches other list views rather than being an invented
        shade. Without a palette the rows stay plain and problems are marked
        by an underline alone, which reads correctly in any theme.
        """
        stripes = theme.striping_colours()
        if stripes:
            self.tag_configure("evenrow", background=stripes["even"])
            self.tag_configure("oddrow", background=stripes["odd"])
        problem_fg = theme.problem_foreground()
        if problem_fg:
            self.tag_configure("problem", font=self._underlined_font(),
                               foreground=problem_fg)
        else:
            self.tag_configure("problem", font=self._underlined_font())

    @staticmethod
    def _underlined_font() -> tkfont.Font:
        f = tkfont.nametofont("TkDefaultFont").copy()
        f.configure(underline=True)
        return f

    def repopulate(self, rows: Sequence) -> None:
        """Replace the contents, keeping selection and scroll where possible."""
        selected = set(self.selection())
        try:
            first_visible = self.yview()[0]
        except tk.TclError:
            first_visible = 0.0

        self.delete(*self.get_children(""))
        for index, row in enumerate(rows):
            tags = ["evenrow" if index % 2 == 0 else "oddrow"]
            if getattr(row, "problem", ""):
                tags.append("problem")
            self.insert("", tk.END, iid=row.iid, values=row.values, tags=tuple(tags))

        still_there = [iid for iid in selected if self.exists(iid)]
        if still_there:
            self.selection_set(still_there)
        if self._sort_column:
            self._sort_by(self._sort_column)
            self._sort_reverse = not self._sort_reverse  # _sort_by flipped it
        try:
            self.yview_moveto(first_visible)
        except tk.TclError:
            pass
