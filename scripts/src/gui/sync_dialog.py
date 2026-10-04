"""
The sync view: everything about the repository, and the four safe actions.

Design notes worth keeping.

*Disabled buttons say why.* Each action sits in a grid row next to a label
that carries the reason straight out of `vcs.RepoStatus.blockers()`. A greyed
button with no explanation is the thing this exists to avoid -- especially
here, where the reasons are genuinely interesting ("commit first", "the
branch has diverged, reconcile it in a terminal").

*Every git call runs on a worker thread*, with the queue plus `after`
polling already used by the import dialog, and only one at a time. A fetch
over a slow link would otherwise freeze the window.

*The audit gates the push.* It runs as soon as the view opens, in the
background, because finding out that the library is broken at the moment you
press Push is too late to be useful. Errors do not make a push impossible --
the override checkbox is there, deliberately, because this is one person's
library on two machines -- but they do make it a decision.

*A pull re-scans and re-audits.* This is the whole point of the feature: what
arrives from the other machine may be a part whose master tables were never
regenerated, and nothing else in the workflow would notice.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Callable, List, Optional, Tuple

from ..core import check as check_mod
from ..core import vcs
from . import settings as st
from .controller import Controller
from .widgets import modal

DEFAULT_GEOMETRY = "980x760"
MIN_WIDTH = 760
MIN_HEIGHT = 600

PAD = 8
POLL_MS = 50

# Job names, used as the queue's discriminator and in the log.
JOB_AUDIT = "audit"
JOB_FETCH = "fetch"
JOB_PULL = "pull"
JOB_COMMIT = "commit"
JOB_PUSH = "push"
JOB_COMMIT_PUSH = "commit_push"


class SyncDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        controller: Controller,
        *,
        settings: Optional[st.Settings] = None,
        on_changed: Optional[Callable[[], None]] = None,
    ):
        super().__init__(parent)
        self.controller = controller
        self.settings = settings
        self.on_changed = on_changed
        self.title("Sync with the remote")
        self.minsize(MIN_WIDTH, MIN_HEIGHT)
        self._restore_geometry()

        self._queue: "queue.Queue[Tuple[str, str, object]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._busy = ""
        # Audit errors, once known. None means "not audited yet", which is a
        # different thing from "no errors" and the push row says so.
        self._audit_errors: Optional[List[check_mod.Finding]] = None
        self._message_edited = False

        body = ttk.Frame(self, padding=PAD)
        body.pack(fill=tk.BOTH, expand=True)
        self._build_header(body)
        self._build_tabs(body)
        self._build_commit(body)
        self._build_actions(body)

        self.protocol("WM_DELETE_WINDOW", self._close)
        modal(self, parent)
        self.refresh()
        self._start(JOB_AUDIT, self._audit_job)
        self.after(POLL_MS, self._drain_queue)

    # -- geometry ----------------------------------------------------------
    def _restore_geometry(self) -> None:
        saved = None
        if self.settings is not None:
            saved = self.settings.window_geometry(
                st.Settings.profile_key(self), st.WINDOW_SYNC
            )
        parsed = st.parse_geometry(saved) if saved else None
        if parsed is None:
            self.geometry(DEFAULT_GEOMETRY)
            return
        self.geometry(st.clamp_geometry(
            parsed, self.winfo_screenwidth(), self.winfo_screenheight(),
            min_width=MIN_WIDTH, min_height=MIN_HEIGHT,
        ))

    def _remember_geometry(self) -> None:
        if self.settings is None or not self.winfo_ismapped():
            return
        parsed = st.parse_geometry(self.geometry())
        if parsed is None or parsed["width"] < MIN_WIDTH:
            return
        self.settings.set_window_geometry(
            st.Settings.profile_key(self), st.WINDOW_SYNC, self.geometry()
        )
        self.settings.save()

    def _close(self) -> None:
        self._remember_geometry()
        self.destroy()

    # -- construction ------------------------------------------------------
    @staticmethod
    def _bold() -> tkfont.Font:
        f = tkfont.nametofont("TkDefaultFont").copy()
        f.configure(weight="bold")
        return f

    def _build_header(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Repository", padding=PAD)
        frame.pack(fill=tk.X)
        frame.columnconfigure(1, weight=1)

        self._header_vars = {}
        for row, (key, label) in enumerate((
            ("state", "State"),
            ("upstream", "Upstream"),
            ("remote", "Remote"),
            ("head", "HEAD"),
            ("fetched", "Fetched"),
        )):
            ttk.Label(frame, text=f"{label}:").grid(
                row=row, column=0, sticky=tk.W, padx=(0, PAD))
            var = tk.StringVar(value="")
            widget = ttk.Label(frame, textvariable=var, anchor=tk.W)
            widget.grid(row=row, column=1, sticky=tk.EW)
            self._header_vars[key] = var
            if key == "state":
                self._state_label = widget
                widget.config(font=self._bold())

    def _build_tabs(self, parent: ttk.Frame) -> None:
        self.tabs = ttk.Notebook(parent)
        self.tabs.pack(fill=tk.BOTH, expand=True, pady=(PAD, 0))

        self.incoming_list = self._commit_list(self.tabs)
        self.outgoing_list = self._commit_list(self.tabs)
        self.tree_list = self._tree_list(self.tabs)
        self.log = self._log_pane(self.tabs)

        self.tabs.add(self.incoming_list.master, text="Incoming")
        self.tabs.add(self.outgoing_list.master, text="Outgoing")
        self.tabs.add(self.tree_list.master, text="Working tree")
        self.tabs.add(self.log.master, text="Log")

    def _scrolled(self, parent: tk.Misc) -> ttk.Frame:
        return ttk.Frame(parent, padding=PAD)

    def _commit_list(self, parent: tk.Misc) -> ttk.Treeview:
        holder = self._scrolled(parent)
        scroll = ttk.Scrollbar(holder, orient=tk.VERTICAL)
        tree = ttk.Treeview(holder, columns=("Commit", "Subject", "Author", "When"),
                            show="headings", yscrollcommand=scroll.set, height=6)
        scroll.config(command=tree.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for name, width in (("Commit", 90), ("Subject", 460),
                            ("Author", 140), ("When", 140)):
            tree.heading(name, text=name)
            tree.column(name, width=width, stretch=(name == "Subject"))
        return tree

    def _tree_list(self, parent: tk.Misc) -> ttk.Treeview:
        holder = self._scrolled(parent)
        scroll = ttk.Scrollbar(holder, orient=tk.VERTICAL)
        tree = ttk.Treeview(holder, columns=("Status", "Path"), show="headings",
                            yscrollcommand=scroll.set, height=6)
        scroll.config(command=tree.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for name, width in (("Status", 140), ("Path", 620)):
            tree.heading(name, text=name)
            tree.column(name, width=width, stretch=(name == "Path"))
        return tree

    def _log_pane(self, parent: tk.Misc) -> tk.Text:
        holder = self._scrolled(parent)
        scroll = ttk.Scrollbar(holder, orient=tk.VERTICAL)
        text = tk.Text(holder, wrap=tk.NONE, height=6,
                       font=tkfont.nametofont("TkFixedFont"),
                       yscrollcommand=scroll.set)
        scroll.config(command=text.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # Readable and copyable, never typed into: this is git's own output.
        text.config(state=tk.DISABLED)
        return text

    def _build_commit(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Commit message", padding=PAD)
        frame.pack(fill=tk.X, pady=(PAD, 0))
        self.message = tk.Text(frame, height=4, wrap=tk.WORD)
        self.message.pack(fill=tk.X)
        # Any keystroke means the user owns the message from then on, and a
        # refresh must not overwrite what they typed.
        self.message.bind("<Key>", self._on_message_key)
        row = ttk.Frame(frame)
        row.pack(fill=tk.X, pady=(PAD // 2, 0))
        self.suggest_button = ttk.Button(row, text="Use suggested message",
                                        command=self._use_suggestion)
        self.suggest_button.pack(side=tk.LEFT)
        self.files_label = ttk.Label(row, text="")
        self.files_label.pack(side=tk.RIGHT)

    def _build_actions(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent, padding=(0, PAD, 0, 0))
        frame.pack(fill=tk.X)
        frame.columnconfigure(1, weight=1)

        self.buttons = {}
        self.reasons = {}
        rows = (
            (JOB_FETCH, "Fetch", self._on_fetch),
            (JOB_PULL, "Pull", self._on_pull),
            (JOB_COMMIT, "Commit", self._on_commit),
            (JOB_COMMIT_PUSH, "Commit & Push", self._on_commit_and_push),
            (JOB_PUSH, "Push", self._on_push),
        )
        for index, (key, label, command) in enumerate(rows):
            button = ttk.Button(frame, text=label, width=16, command=command)
            button.grid(row=index, column=0, sticky=tk.W, pady=1)
            reason = ttk.Label(frame, text="", anchor=tk.W, wraplength=640,
                               justify=tk.LEFT)
            reason.grid(row=index, column=1, sticky=tk.EW, padx=(PAD, 0))
            self.buttons[key] = button
            self.reasons[key] = reason

        self.push_anyway = tk.BooleanVar(value=False)
        self.push_anyway_check = ttk.Checkbutton(
            frame, text="Push anyway", variable=self.push_anyway,
            command=self._update_actions,
        )
        self.push_anyway_check.grid(row=len(rows), column=0, columnspan=2,
                                    sticky=tk.W, pady=(PAD // 2, 0))

        closing = ttk.Frame(parent)
        closing.pack(fill=tk.X, pady=(PAD, 0))
        self.busy_label = ttk.Label(closing, text="")
        self.busy_label.pack(side=tk.LEFT)
        ttk.Button(closing, text="Close", command=self._close).pack(side=tk.RIGHT)
        ttk.Button(closing, text="Refresh",
                   command=self.refresh).pack(side=tk.RIGHT, padx=(0, PAD // 2))

    # -- rendering ---------------------------------------------------------
    def refresh(self) -> None:
        """Re-read the status and repopulate everything from it."""
        status = self.controller.git_status(refresh=True)
        self._status = status

        self._header_vars["state"].set(f"{status.state}  —  {status.state_label}")
        self._header_vars["upstream"].set(
            f"{status.upstream}  (ahead {status.ahead}, behind {status.behind})"
            if status.upstream else "none"
        )
        self._header_vars["remote"].set(
            f"{status.remote_name}  →  {status.remote_url}"
            if status.remote_url else (status.remote_name or "none")
        )
        if status.head:
            self._header_vars["head"].set(
                f"{status.head.short}  {status.head.subject}\n"
                f"{status.head.author}, {status.head.date_relative}  "
                f"({status.head.sha})"
            )
        else:
            self._header_vars["head"].set("(no commits yet)")
        self._header_vars["fetched"].set(vcs.describe_fetch_age(status.last_fetch))

        incoming = self.controller.git_incoming()
        outgoing = self.controller.git_outgoing()
        self._fill_commits(self.incoming_list, incoming)
        self._fill_commits(self.outgoing_list, outgoing)
        self.tabs.tab(0, text=f"Incoming ({len(incoming)})")
        self.tabs.tab(1, text=f"Outgoing ({len(outgoing)})")

        preview = self.controller.git_preview_commit()
        self._fill_tree(status, preview)
        self.tabs.tab(2, text=f"Working tree ({len(preview.files)})")
        self.files_label.config(text=f"{len(preview.files)} path(s) would be committed")

        if not self._message_edited:
            self._set_message(preview.message)
        for warning in preview.warnings:
            self._append_log(f"warning: {warning}")
        self._update_actions()

    def _fill_commits(self, tree: ttk.Treeview, commits: List[vcs.Commit]) -> None:
        tree.delete(*tree.get_children(""))
        for commit in commits:
            tree.insert("", tk.END, values=(commit.short, commit.subject,
                                            commit.author, commit.date_relative))

    def _fill_tree(self, status: vcs.RepoStatus, preview: vcs.CommitPreview) -> None:
        labels = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed",
                  "C": "copied", "?": "untracked", "U": "conflicted", "T": "type change"}
        self.tree_list.delete(*self.tree_list.get_children(""))
        # Conflicts first: they are the one thing here that must be acted on.
        ordering = {"U": 0, "D": 1, "R": 2, "M": 3, "A": 4, "?": 5}
        for letter, path in sorted(preview.files,
                                   key=lambda item: (ordering.get(item[0], 9), item[1])):
            self.tree_list.insert("", tk.END,
                                  values=(labels.get(letter, letter), path))

    def _set_message(self, text: str) -> None:
        self.message.delete("1.0", tk.END)
        self.message.insert("1.0", text)

    def _on_message_key(self, _event=None) -> None:
        self._message_edited = True

    def _use_suggestion(self) -> None:
        self._set_message(self.controller.git_suggested_message())
        self._message_edited = False

    def _commit_message(self) -> str:
        return self.message.get("1.0", tk.END).strip()

    def _append_log(self, text: str) -> None:
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, text.rstrip() + "\n")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    # -- enabling ----------------------------------------------------------
    def audit_reason(self) -> str:
        """
        Why the audit currently stands in the way of a push, if it does.

        Kept as a method so the whole rule is readable in one place, and
        testable: "not audited yet" is a distinct state from "no errors".
        """
        if self._audit_errors is None:
            return "Auditing the library…"
        if not self._audit_errors:
            return ""
        count = len(self._audit_errors)
        if self.push_anyway.get():
            return ""
        return (f"The audit found {count} error(s). Fix them, or tick "
                f"“Push anyway”.")

    def _update_actions(self) -> None:
        status = self._status
        blockers = status.blockers()

        count = len(self._audit_errors or ())
        self.push_anyway_check.config(
            text=f"Push anyway ({count} error(s))" if count else "Push anyway"
        )
        if count:
            self.push_anyway_check.state(["!disabled"])
        else:
            # Nothing to override, so offering the override is noise.
            self.push_anyway.set(False)
            self.push_anyway_check.state(["disabled"])

        reasons = {
            JOB_FETCH: blockers.get(vcs.FETCH, ""),
            JOB_PULL: blockers.get(vcs.PULL, ""),
            JOB_COMMIT: blockers.get(vcs.COMMIT, ""),
            JOB_PUSH: blockers.get(vcs.PUSH, "") or self.audit_reason(),
        }
        # Commit & Push needs both to be possible; whichever objects first is
        # the reason worth showing.
        reasons[JOB_COMMIT_PUSH] = reasons[JOB_COMMIT] or self.audit_reason() or (
            "" if status.upstream else blockers.get(vcs.PUSH, "")
        )

        if not reasons[JOB_COMMIT] and not self._commit_message():
            for key in (JOB_COMMIT, JOB_COMMIT_PUSH):
                reasons[key] = "A commit needs a message."

        # While a job is running everything is disabled, and the reason says
        # which job -- a button greyed out with a blank line beside it is
        # exactly the thing this layout exists to prevent.
        busy_reason = (f"Waiting for the {self._busy.replace('_', ' ')} to finish…"
                       if self._busy else "")
        for key, reason in reasons.items():
            if self._busy:
                self.buttons[key].config(state=tk.DISABLED)
                self.reasons[key].config(text=reason or busy_reason)
                continue
            self.buttons[key].config(state=tk.DISABLED if reason else tk.NORMAL)
            self.reasons[key].config(text=reason)

        self.busy_label.config(text=busy_reason)

    # -- worker ------------------------------------------------------------
    def _start(self, job: str, work: Callable[[], object]) -> bool:
        """
        Run `work` off the UI thread. Refuses while another job is running.

        One at a time is not laziness: two concurrent git commands on one
        repository contend for the index lock, and the second fails with
        something no user should have to read.
        """
        if self._busy:
            return False
        self._busy = job

        def runner() -> None:
            try:
                self._queue.put((job, "done", work()))
            except Exception as exc:  # noqa: BLE001 -- surfaced on the UI thread
                self._queue.put((job, "error", exc))

        self._worker = threading.Thread(target=runner, daemon=True)
        self._worker.start()
        self._update_actions()
        return True

    def _drain_queue(self) -> None:
        try:
            while True:
                job, kind, payload = self._queue.get_nowait()
                self._busy = ""
                if kind == "error":
                    self._append_log(f"ERROR: {payload}")
                    messagebox.showerror(f"{job} failed", str(payload), parent=self)
                else:
                    self._finish(job, payload)
                self._update_actions()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(POLL_MS, self._drain_queue)

    def _finish(self, job: str, payload: object) -> None:
        if job == JOB_AUDIT:
            self._audit_errors = list(payload)   # type: ignore[arg-type]
            if self._audit_errors:
                self._append_log(
                    f"audit: {len(self._audit_errors)} error(s) -- a push is held "
                    f"back until they are fixed or overridden")
                for finding in self._audit_errors:
                    self._append_log(f"  {finding.format()}")
            else:
                self._append_log("audit: no errors")
            return

        results = payload if isinstance(payload, list) else [payload]
        for result in results:
            self._append_log(result.transcript())    # type: ignore[union-attr]
        failed = [r for r in results if not r.ok]    # type: ignore[union-attr]

        if job in (JOB_COMMIT, JOB_COMMIT_PUSH) and not failed:
            self._message_edited = False
        if job in (JOB_PULL, JOB_COMMIT_PUSH, JOB_COMMIT) and not failed:
            if self.on_changed is not None:
                self.on_changed()
        if job == JOB_PULL and not failed:
            # Re-audit what arrived. The two-machine failure this exists for
            # is a pulled part whose master tables were never regenerated.
            self._audit_errors = None
            self._append_log("re-auditing after the pull…")
            self.refresh()
            self._start(JOB_AUDIT, self._audit_job)
            return
        if failed:
            self._append_log(f"{job} did not complete: {failed[0].message}")
        self.refresh()

    # -- jobs --------------------------------------------------------------
    def _audit_job(self) -> List[check_mod.Finding]:
        return self.controller.audit_blocks_push()

    def _on_fetch(self) -> None:
        self._start(JOB_FETCH, lambda: self.controller.git_fetch())

    def _on_pull(self) -> None:
        self._start(JOB_PULL, lambda: self.controller.git_pull())

    def _on_commit(self) -> None:
        message = self._commit_message()
        if not message:
            return
        self._start(JOB_COMMIT, lambda: self.controller.git_commit(message))

    def _on_push(self) -> None:
        skip = self.push_anyway.get()
        self._start(JOB_PUSH, lambda: self.controller.git_push(skip_check=skip))

    def _on_commit_and_push(self) -> None:
        message = self._commit_message()
        if not message:
            return
        skip = self.push_anyway.get()

        def work() -> List[vcs.GitResult]:
            committed = self.controller.git_commit(message)
            if not committed.ok:
                return [committed]
            return [committed, self.controller.git_push(skip_check=skip)]

        self._start(JOB_COMMIT_PUSH, work)
