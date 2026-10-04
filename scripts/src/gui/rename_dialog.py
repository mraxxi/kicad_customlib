"""
Rename and move dialogs.

Both are previews with a name field attached: the plan, with its count of
references that will be rewritten and its warning about projects breaking, is
the point of the dialog rather than an afterthought.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk
from typing import Callable, Optional

from ..core import naming
from ..core import ops
from .controller import Controller, Row
from .widgets import (GAP, GAP_M, PAD_DIALOG, PAD_SECTION, CategoryPicker,
                      PlanPreview, modal)


class _PlanDialog(tk.Toplevel):
    """Shared shell: inputs at the top, a live plan summary below."""

    def __init__(self, parent: tk.Misc, controller: Controller, title: str):
        super().__init__(parent)
        self.title(title)
        self.controller = controller
        self.applied = False
        self.geometry("760x560")
        self.minsize(560, 420)

        self.body = ttk.Frame(self, padding=PAD_DIALOG)
        self.body.pack(fill=tk.BOTH, expand=True)

        self.inputs = ttk.Frame(self.body)
        self.inputs.pack(fill=tk.X)

        self.feedback = ttk.Label(self.body, text="", wraplength=700,
                                  justify=tk.LEFT)
        self.feedback.pack(fill=tk.X, pady=(GAP, 0))

        preview_frame = ttk.LabelFrame(self.body, text="What will happen",
                                       padding=PAD_SECTION)
        preview_frame.pack(fill=tk.BOTH, expand=True, pady=(GAP, 0))
        scroll = ttk.Scrollbar(preview_frame, orient=tk.VERTICAL)
        self.preview = tk.Text(preview_frame, wrap=tk.NONE, height=14,
                               font=tkfont.nametofont("TkFixedFont"),
                               yscrollcommand=scroll.set)
        scroll.config(command=self.preview.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.preview.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.preview.config(state=tk.DISABLED)

        buttons = ttk.Frame(self.body)
        buttons.pack(fill=tk.X, pady=(GAP_M, 0))
        self.apply_button = ttk.Button(buttons, text="Apply", state=tk.DISABLED,
                                       command=self._apply)
        self.apply_button.pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(
            side=tk.RIGHT, padx=(0, GAP))

        self._plan: Optional[ops.Plan] = None
        self.bind("<Return>", lambda _e: self._apply())

    def _set_preview(self, text: str) -> None:
        self.preview.config(state=tk.NORMAL)
        self.preview.delete("1.0", tk.END)
        self.preview.insert("1.0", text)
        self.preview.config(state=tk.DISABLED)

    def _rebuild(self) -> None:
        """Recompute the plan from the current inputs. Never touches the disk."""
        self._plan = None
        self.apply_button.config(state=tk.DISABLED)
        try:
            plan = self.build_plan()
        except (naming.NameError_, ValueError) as exc:
            self.feedback.config(text=str(exc))
            self._set_preview("")
            return
        except Exception as exc:  # noqa: BLE001 -- shown, not swallowed
            self.feedback.config(text=f"{type(exc).__name__}: {exc}")
            self._set_preview("")
            return

        if plan is None:
            self.feedback.config(text="")
            self._set_preview("")
            return

        self._plan = plan
        self.feedback.config(text="")
        self._set_preview(plan.summary())
        if not plan.is_empty:
            self.apply_button.config(state=tk.NORMAL)

    def build_plan(self) -> Optional[ops.Plan]:
        raise NotImplementedError

    def _apply(self) -> None:
        if self._plan is None or self._plan.is_empty:
            return
        result = self.controller.apply(self._plan)
        if not result.ok:
            messagebox.showerror("Failed",
                                 result.summary(self.controller.root), parent=self)
            return
        self.applied = True
        self.result_message = result.summary(self.controller.root).splitlines()[0]
        self.destroy()


class RenameDialog(_PlanDialog):
    """Rename one item, or a whole category."""

    def __init__(self, parent: tk.Misc, controller: Controller, row: Row):
        super().__init__(parent, controller, f"Rename {row.kind} {row.name}")
        self.row = row

        ttk.Label(self.inputs, text=f"Current name:").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(self.inputs, text=row.name).grid(row=0, column=1, sticky=tk.W,
                                                   padx=GAP)

        ttk.Label(self.inputs, text="New name:").grid(row=1, column=0, sticky=tk.W,
                                                      pady=(GAP, 0))
        self.new_name = tk.StringVar(value=row.name)
        entry = ttk.Entry(self.inputs, textvariable=self.new_name, width=40)
        entry.grid(row=1, column=1, sticky=tk.EW, padx=GAP, pady=(GAP, 0))
        entry.focus_set()
        entry.selection_range(0, tk.END)

        self.rename_model = tk.BooleanVar(value=False)
        if row.kind == "footprint":
            ttk.Checkbutton(
                self.inputs,
                text="Also rename the 3D model file to match",
                variable=self.rename_model, command=self._rebuild,
            ).grid(row=2, column=1, sticky=tk.W, padx=GAP, pady=(GAP, 0))

        self.conflict = tk.StringVar(value=ops.ConflictPolicy.SKIP.value)
        ttk.Label(self.inputs, text="If the target exists:").grid(
            row=3, column=0, sticky=tk.W, pady=(GAP, 0))
        ttk.Combobox(self.inputs, textvariable=self.conflict, state="readonly",
                     width=12, values=[p.value for p in ops.ConflictPolicy]).grid(
            row=3, column=1, sticky=tk.W, padx=GAP, pady=(GAP, 0))

        self.inputs.columnconfigure(1, weight=1)
        self.new_name.trace_add("write", lambda *_a: self._rebuild())
        self.conflict.trace_add("write", lambda *_a: self._rebuild())
        self._rebuild()
        modal(self, parent)

    def build_plan(self) -> Optional[ops.Plan]:
        new = self.new_name.get().strip()
        if not new or new == self.row.name:
            return None
        return self.controller.plan_rename(
            self.row.kind, self.row.category, self.row.name, new,
            rename_model=self.rename_model.get(),
            conflict=ops.ConflictPolicy(self.conflict.get()),
        )


class MoveDialog(_PlanDialog):
    """Move one item into another category."""

    def __init__(self, parent: tk.Misc, controller: Controller, row: Row):
        super().__init__(parent, controller, f"Move {row.kind} {row.name}")
        self.row = row

        ttk.Label(self.inputs, text="Item:").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(self.inputs, text=f"{row.category}:{row.name}").grid(
            row=0, column=1, sticky=tk.W, padx=GAP)

        ttk.Label(self.inputs, text="Move to:").grid(row=1, column=0, sticky=tk.W,
                                                     pady=(GAP, 0))
        self.picker = CategoryPicker(
            self.inputs, [c for c in controller.categories() if c != row.category],
            validate=controller.validate_new_category,
            on_change=lambda _n: self._rebuild(),
        )
        self.picker.grid(row=1, column=1, sticky=tk.W, padx=GAP, pady=(GAP, 0))

        self.conflict = tk.StringVar(value=ops.ConflictPolicy.SKIP.value)
        ttk.Label(self.inputs, text="If the target exists:").grid(
            row=2, column=0, sticky=tk.W, pady=(GAP, 0))
        ttk.Combobox(self.inputs, textvariable=self.conflict, state="readonly",
                     width=12, values=[p.value for p in ops.ConflictPolicy]).grid(
            row=2, column=1, sticky=tk.W, padx=GAP, pady=(GAP, 0))

        self.inputs.columnconfigure(1, weight=1)
        self.conflict.trace_add("write", lambda *_a: self._rebuild())
        self._rebuild()
        modal(self, parent)

    def build_plan(self) -> Optional[ops.Plan]:
        target = self.picker.get()
        if not target or target == self.row.category:
            return None
        return self.controller.plan_move(
            self.row.kind, self.row.category, self.row.name, target,
            conflict=ops.ConflictPolicy(self.conflict.get()),
        )


class CategoryRenameDialog(_PlanDialog):
    """Rename a whole category -- all three directories and every reference."""

    def __init__(self, parent: tk.Misc, controller: Controller, category: str):
        super().__init__(parent, controller, f"Rename category {category}")
        self.category = category

        ttk.Label(self.inputs, text="Category:").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(self.inputs, text=category).grid(row=0, column=1, sticky=tk.W,
                                                   padx=GAP)

        ttk.Label(self.inputs, text="New name:").grid(row=1, column=0, sticky=tk.W,
                                                      pady=(GAP, 0))
        self.new_name = tk.StringVar(value=category)
        entry = ttk.Entry(self.inputs, textvariable=self.new_name, width=40)
        entry.grid(row=1, column=1, sticky=tk.EW, padx=GAP, pady=(GAP, 0))
        entry.focus_set()
        entry.selection_range(0, tk.END)

        self.inputs.columnconfigure(1, weight=1)
        self.new_name.trace_add("write", lambda *_a: self._rebuild())
        self._rebuild()
        modal(self, parent)

    def build_plan(self) -> Optional[ops.Plan]:
        new = self.new_name.get().strip()
        if not new or new == self.category:
            return None
        return self.controller.plan_rename("category", "", self.category, new)
