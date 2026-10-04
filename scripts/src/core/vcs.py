"""
Git, as the library manager needs it: read the state, and perform the four
safe operations.

This is `core/` rather than `gui/` for two reasons. The CLI uses it as well,
and keeping every decision here means the whole of it is testable against a
temporary repository with no display and no network.

**What this module will never do.** It issues only `status`, `rev-parse`,
`symbolic-ref`, `log`, `rev-list`, `remote`, `add`, `commit`, `fetch`,
`pull --ff-only` and `push`. There is no force push, no reset, no checkout,
no stash, no rebase, no explicit merge, no clean and no gc, because every one
of those can discard work that exists nowhere else. A two-machine library is
exactly the situation where that work is irreplaceable. Anything that needs
them is a job for a terminal, and the UI says so when it refuses.
`tests/test_vcs.py` asserts that no such subcommand appears below.

Nothing here raises on a git failure. Every call returns a `GitResult`
carrying the exit code and both streams, so a caller can show the user what
git actually said instead of a traceback.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import library as lb

# A local read is either instant or something is wrong -- a held index lock,
# a network filesystem that has gone away. Transfers legitimately take a
# while on a repository holding STEP files.
LOCAL_TIMEOUT_S = 10
NETWORK_TIMEOUT_S = 120

# Returned when git never finished. 124 is the convention `timeout(1)` uses.
EXIT_TIMEOUT = 124
# Returned when git could not be run at all.
EXIT_UNAVAILABLE = 127

# Record and field separators for `log --format`. Chosen because neither can
# occur in a commit subject or an author name.
_RS = "\x1e"
_FS = "\x1f"
_LOG_FORMAT = _FS.join(("%H", "%h", "%s", "%an", "%aI", "%ar")) + _RS

# States, in the precedence `RepoStatus.state` applies them.
NOT_A_REPO = "not_a_repo"
OPERATION_IN_PROGRESS = "operation_in_progress"
UNMERGED = "unmerged"
DETACHED = "detached"
NO_UPSTREAM = "no_upstream"
DIVERGED = "diverged"
BEHIND = "behind"
AHEAD = "ahead"
DIRTY = "dirty"
IN_SYNC = "in_sync"

STATE_LABELS = {
    NOT_A_REPO: "not a git repository",
    OPERATION_IN_PROGRESS: "a git operation is in progress",
    UNMERGED: "unresolved conflicts",
    DETACHED: "detached HEAD",
    NO_UPSTREAM: "no upstream branch",
    DIVERGED: "diverged from upstream",
    BEHIND: "behind upstream",
    AHEAD: "ahead of upstream",
    DIRTY: "uncommitted changes",
    IN_SYNC: "in sync",
}

# Actions `blockers()` reports on.
FETCH = "fetch"
PULL = "pull"
COMMIT = "commit"
PUSH = "push"

# Anything over this in a commit is worth mentioning: git stores a binary
# blob whole, in every clone, forever, and this repository has no LFS.
LARGE_FILE_WARN_BYTES = 8 * 1024 * 1024


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GitResult:
    """One git invocation: what was asked, and everything it said."""
    command: Tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def message(self) -> str:
        """The most useful single line for a status bar."""
        for stream in (self.stderr, self.stdout):
            for line in stream.splitlines():
                if line.strip():
                    return line.strip()
        return "ok" if self.ok else f"git exited with {self.returncode}"

    def transcript(self) -> str:
        """Everything, for the log pane."""
        out = ["$ git " + " ".join(self.command)]
        if self.stdout.strip():
            out.append(self.stdout.rstrip())
        if self.stderr.strip():
            out.append(self.stderr.rstrip())
        if not self.ok:
            out.append(f"(exit {self.returncode})")
        return "\n".join(out)


@dataclass(frozen=True)
class Commit:
    sha: str
    short: str
    subject: str
    author: str
    date_iso: str
    date_relative: str

    def describe(self) -> str:
        return f"{self.short}  {self.subject}"


@dataclass(frozen=True)
class StatusEntry:
    """
    One line of `git status --porcelain=v1`.

    `x` is the index column and `y` the working-tree column; keeping both,
    rather than only the derived lists, is what lets a commit preview show
    the same letters git would.
    """
    x: str
    y: str
    path: str
    orig_path: str = ""

    @property
    def untracked(self) -> bool:
        return self.x == "?"

    @property
    def unmerged(self) -> bool:
        return self.x == "U" or self.y == "U" or (self.x + self.y) in ("DD", "AA")

    @property
    def letter(self) -> str:
        if self.unmerged:
            return "U"
        if self.untracked:
            return "?"
        return self.x if self.x != " " else self.y


@dataclass
class RepoStatus:
    is_repo: bool = False
    branch: Optional[str] = None
    detached: bool = False
    head: Optional[Commit] = None
    upstream: Optional[str] = None
    remote_name: Optional[str] = None
    remote_url: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    entries: List[StatusEntry] = field(default_factory=list)
    staged: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    untracked: List[str] = field(default_factory=list)
    unmerged: List[str] = field(default_factory=list)
    operation: Optional[str] = None
    last_fetch: Optional[datetime] = None
    error: str = ""

    @property
    def dirty(self) -> bool:
        return bool(self.staged or self.modified or self.deleted
                    or self.untracked or self.unmerged)

    @property
    def changed_count(self) -> int:
        """Distinct paths with something to report."""
        return len({e.path for e in self.entries})

    @property
    def state(self) -> str:
        if not self.is_repo:
            return NOT_A_REPO
        if self.operation:
            return OPERATION_IN_PROGRESS
        if self.unmerged:
            return UNMERGED
        if self.detached:
            return DETACHED
        if not self.upstream:
            return NO_UPSTREAM
        if self.ahead and self.behind:
            return DIVERGED
        if self.behind:
            return BEHIND
        if self.ahead:
            return AHEAD
        if self.dirty:
            return DIRTY
        return IN_SYNC

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    def blockers(self) -> Dict[str, str]:
        """
        Which actions cannot be run, and why.

        The reasons are shown to the user verbatim, so they are written as
        whole sentences. An action absent from this map is available.
        """
        out: Dict[str, str] = {}
        if not self.is_repo:
            reason = "This folder is not a git repository."
            return {FETCH: reason, PULL: reason, COMMIT: reason, PUSH: reason}

        if self.operation:
            reason = (f"A {self.operation} is in progress. Finish or abort it in a "
                      f"terminal first.")
            for action in (PULL, COMMIT, PUSH):
                out[action] = reason
        elif self.unmerged:
            reason = (f"{len(self.unmerged)} file(s) have unresolved conflicts. "
                      f"Resolve them in a terminal first.")
            for action in (PULL, COMMIT, PUSH):
                out[action] = reason

        if not self.remote_name:
            out[FETCH] = "There is no remote configured for this repository."

        if self.detached:
            reason = ("HEAD is detached, so there is no branch to sync. "
                      "Check out a branch in a terminal first.")
            out.setdefault(PULL, reason)
            out.setdefault(PUSH, reason)
        elif not self.upstream:
            reason = ("This branch has no upstream. Set one with "
                      "'git push -u origin <branch>' in a terminal.")
            out.setdefault(PULL, reason)
            out.setdefault(PUSH, reason)

        if PULL not in out:
            if self.ahead and self.behind:
                out[PULL] = (f"The branch has diverged: {self.ahead} local and "
                             f"{self.behind} remote commit(s). This needs a rebase "
                             f"or a merge, which only a terminal should do.")
            elif not self.behind:
                out[PULL] = "There is nothing to pull."
            elif self.dirty:
                # A fast-forward that touches a modified file aborts with a
                # message about overwriting local changes. Refusing here says
                # something useful instead.
                out[PULL] = (f"{self.changed_count} uncommitted change(s) would block a "
                             f"fast-forward. Commit them first.")

        if COMMIT not in out and not self.dirty:
            out[COMMIT] = "There is nothing to commit."

        if PUSH not in out:
            if self.behind and self.ahead:
                out[PUSH] = ("The branch has diverged from upstream. Reconcile it in "
                             "a terminal before pushing.")
            elif self.behind:
                out[PUSH] = (f"Upstream is {self.behind} commit(s) ahead. Pull first.")
            elif not self.ahead:
                out[PUSH] = "There is nothing to push."
            elif self.dirty:
                out[PUSH] = (f"{self.changed_count} uncommitted change(s) would not be "
                             f"included. Commit them first.")

        return out

    def summary_line(self) -> str:
        """
        One glanceable line, e.g.

            main · b6c9373 · ↑2 ↓0 · 3 changed · fetched 4 min ago
        """
        if not self.is_repo:
            return ""
        bits: List[str] = [self.branch or (self.head.short if self.head else "(no commits)")]
        if self.detached:
            bits[0] = f"detached at {self.head.short}" if self.head else "detached"
        if self.head and not self.detached:
            bits.append(self.head.short)
        if self.upstream:
            bits.append(f"↑{self.ahead} ↓{self.behind}")
        else:
            bits.append("no upstream")
        if self.dirty:
            bits.append(f"{self.changed_count} changed")
        else:
            bits.append("clean")
        bits.append(describe_fetch_age(self.last_fetch))
        return " · ".join(bits)


@dataclass
class CommitPreview:
    """
    What a commit would contain.

    Deliberately *not* an `ops.Plan`: a Plan models interlinked filesystem
    operations on fragile S-expression files, which is why it exists at all.
    A commit is a single git invocation over a list of paths, and dressing it
    up as a Plan would only obscure that.
    """
    message: str = ""
    files: List[Tuple[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.files

    def summary(self) -> str:
        if self.is_empty:
            return "Nothing to commit."
        lines = [f"Commit {len(self.files)} path(s):"]
        lines += [f"  {letter}  {path}" for letter, path in self.files]
        if self.message:
            lines.append("")
            lines.append("Message:")
            lines += [f"  {line}" for line in self.message.splitlines()]
        for warning in self.warnings:
            lines.append(f"  warning: {warning}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Running git
# --------------------------------------------------------------------------

def _env() -> Dict[str, str]:
    """
    An environment in which git cannot stop and wait for a human.

    Without this, a repository whose remote needs a password turns any call
    into a hang: the credential helper reads from a terminal that the GUI
    does not have, and the dialog never comes back.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    # An askpass helper falls back to a graphical prompt if it can see a
    # display, so take that away too.
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    # Reading status must never take the index lock: doing so would make a
    # background refresh collide with the user's own terminal.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    # Parsing depends on git's own wording in a few places, and on the C
    # collation order everywhere.
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def _run(root: Path, *args: str, timeout: int = LOCAL_TIMEOUT_S) -> GitResult:
    """
    Run one git command inside `root`. Never raises.

    `-C root` rather than `cwd=` so the command is reproducible from the
    transcript the UI shows.
    """
    command = ("-C", str(root)) + args
    try:
        proc = subprocess.run(
            ["git", *command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return GitResult(
            command, EXIT_TIMEOUT, "",
            f"git {args[0] if args else ''} did not finish within {timeout}s "
            f"and was stopped. Nothing was changed.",
        )
    except (OSError, ValueError) as exc:
        return GitResult(command, EXIT_UNAVAILABLE, "",
                         f"git could not be run: {exc}")
    return GitResult(command, proc.returncode, proc.stdout, proc.stderr)


def available() -> bool:
    """Whether a usable git exists at all."""
    return _run(Path.cwd(), "--version").ok


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def _first_line(result: GitResult) -> str:
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""


def _parse_status_z(payload: str) -> List[StatusEntry]:
    """
    Parse `status --porcelain=v1 -z`.

    `-z` rather than the default because the plain form quotes and escapes
    any path with a space or a non-ASCII byte, and this library is full of
    both (`JST_B4B-ZR_LF__SN_`, vendor names with accents). With `-z` the
    path is literal; the cost is that a rename emits two NUL-separated
    fields for one entry, so the fields cannot simply be zipped.
    """
    fields = payload.split("\0")
    entries: List[StatusEntry] = []
    i = 0
    while i < len(fields):
        chunk = fields[i]
        i += 1
        if len(chunk) < 4:
            # The trailing empty field after the final NUL, or junk.
            continue
        x, y, path = chunk[0], chunk[1], chunk[3:]
        orig = ""
        if ("R" in (x, y) or "C" in (x, y)) and i < len(fields):
            # For a rename git emits "<new>\0<old>"; the old name is the
            # extra field.
            orig = fields[i]
            i += 1
        entries.append(StatusEntry(x=x, y=y, path=path, orig_path=orig))
    return entries


def _detect_operation(git_dir: Path) -> Optional[str]:
    if (git_dir / "MERGE_HEAD").exists():
        return "merge"
    if (git_dir / "rebase-merge").is_dir() or (git_dir / "rebase-apply").is_dir():
        return "rebase"
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        return "cherry-pick"
    if (git_dir / "REVERT_HEAD").exists():
        return "revert"
    return None


def _read_head(root: Path) -> Optional[Commit]:
    result = _run(root, "log", "-1", f"--format={_LOG_FORMAT}")
    commits = _parse_commits(result.stdout)
    return commits[0] if commits else None


def _parse_commits(payload: str) -> List[Commit]:
    out: List[Commit] = []
    for record in payload.split(_RS):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split(_FS)
        if len(parts) < 6:
            continue
        sha, short, subject, author, date_iso, date_rel = parts[:6]
        out.append(Commit(sha=sha, short=short, subject=subject, author=author,
                          date_iso=date_iso, date_relative=date_rel))
    return out


def read_status(root: Path) -> RepoStatus:
    """
    Everything the UI needs about the repository, in one pass.

    A non-repository is a normal answer, not an error: the library can
    perfectly well be used without git, and the interface simply hides the
    sync controls.
    """
    status = RepoStatus()
    inside = _run(root, "rev-parse", "--is-inside-work-tree")
    if not inside.ok or _first_line(inside) != "true":
        if inside.returncode in (EXIT_TIMEOUT, EXIT_UNAVAILABLE):
            status.error = inside.message
        return status
    status.is_repo = True

    git_dir_result = _run(root, "rev-parse", "--absolute-git-dir")
    git_dir = Path(_first_line(git_dir_result)) if git_dir_result.ok else root / ".git"
    status.operation = _detect_operation(git_dir)
    try:
        fetch_head = git_dir / "FETCH_HEAD"
        if fetch_head.exists():
            status.last_fetch = datetime.fromtimestamp(fetch_head.stat().st_mtime)
    except OSError:
        pass

    branch = _first_line(_run(root, "symbolic-ref", "--short", "-q", "HEAD"))
    if branch:
        status.branch = branch
    else:
        status.detached = True

    status.head = _read_head(root)

    upstream = _run(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.ok:
        status.upstream = _first_line(upstream) or None
    if status.upstream:
        # The upstream is "<remote>/<branch>", and a remote name may itself
        # contain a slash, so ask git which remote it is rather than split.
        remote = _first_line(_run(root, "config", "--get",
                                  f"branch.{status.branch}.remote"))
        status.remote_name = remote or status.upstream.split("/", 1)[0]
    if not status.remote_name:
        remotes = [r for r in _run(root, "remote").stdout.split() if r]
        if remotes:
            status.remote_name = "origin" if "origin" in remotes else remotes[0]
    if status.remote_name:
        url = _run(root, "remote", "get-url", status.remote_name)
        status.remote_url = _first_line(url) or None

    if status.upstream:
        counts = _run(root, "rev-list", "--left-right", "--count", "HEAD...@{u}")
        parts = counts.stdout.split()
        if counts.ok and len(parts) == 2:
            try:
                status.ahead, status.behind = int(parts[0]), int(parts[1])
            except ValueError:
                pass

    porcelain = _run(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if porcelain.ok:
        status.entries = _parse_status_z(porcelain.stdout)
    else:
        status.error = porcelain.message

    for entry in status.entries:
        if entry.unmerged:
            status.unmerged.append(entry.path)
            continue
        if entry.untracked:
            status.untracked.append(entry.path)
            continue
        if entry.x not in (" ", "?"):
            status.staged.append(entry.path)
        if entry.y == "M":
            status.modified.append(entry.path)
        elif entry.y == "D":
            status.deleted.append(entry.path)

    return status


def incoming(root: Path, limit: int = 50) -> List[Commit]:
    """What a pull would bring down."""
    result = _run(root, "log", f"--max-count={limit}",
                  f"--format={_LOG_FORMAT}", "HEAD..@{u}")
    return _parse_commits(result.stdout) if result.ok else []


def outgoing(root: Path, limit: int = 50) -> List[Commit]:
    """What a push would send up."""
    result = _run(root, "log", f"--max-count={limit}",
                  f"--format={_LOG_FORMAT}", "@{u}..HEAD")
    return _parse_commits(result.stdout) if result.ok else []


def describe_fetch_age(when: Optional[datetime], now: Optional[datetime] = None) -> str:
    """
    How current the ahead/behind counts are.

    This is not decoration. Ahead and behind are computed against the
    remote-tracking ref, which only a fetch updates, so "up to date" after a
    week without one means nothing at all. Saying when the numbers are from
    is the difference between a useful indicator and a misleading one.
    """
    if when is None:
        return "never fetched"
    delta = ((now or datetime.now()) - when).total_seconds()
    if delta < 0:
        return "fetched just now"
    if delta < 90:
        return "fetched just now"
    if delta < 3600:
        return f"fetched {int(delta // 60)} min ago"
    if delta < 86400:
        hours = int(delta // 3600)
        return f"fetched {hours} hour{'s' if hours != 1 else ''} ago"
    days = int(delta // 86400)
    return f"fetched {days} day{'s' if days != 1 else ''} ago"


# --------------------------------------------------------------------------
# Classifying a path, for commit messages and warnings
# --------------------------------------------------------------------------

KIND_SYMBOL = "symbol"
KIND_FOOTPRINT = "footprint"
KIND_MODEL = "model"
KIND_TABLE = "table"
KIND_PROVENANCE = "provenance"
KIND_TEMPLATE = "template"
KIND_SCRIPT = "script"
KIND_TEST = "test"
KIND_DOC = "doc"
KIND_OTHER = "other"

_PLURALS = {
    KIND_SYMBOL: "symbols",
    KIND_FOOTPRINT: "footprints",
    KIND_MODEL: "3D models",
    KIND_TEMPLATE: "template files",
}


@dataclass(frozen=True)
class PathInfo:
    kind: str
    category: str = ""


def classify_path(rel_path: str) -> PathInfo:
    """
    What a repo-relative path is, by position in the tree.

    Derived from the path rather than from a `library.Library` scan, because
    the paths that most need naming in a commit message are the deleted ones,
    and those are by definition no longer there to be scanned.
    """
    parts = Path(rel_path.replace("\\", "/")).parts
    if not parts:
        return PathInfo(KIND_OTHER)
    head = parts[0]

    if head in ("sym-lib-table", "fp-lib-table"):
        return PathInfo(KIND_TABLE)
    if head == "provenance.json":
        return PathInfo(KIND_PROVENANCE)
    if head == "template":
        return PathInfo(KIND_TEMPLATE)
    if head == "scripts":
        return PathInfo(KIND_SCRIPT)
    if head == "tests":
        return PathInfo(KIND_TEST)
    if len(parts) == 1 and head.lower().endswith((".md", ".txt")):
        return PathInfo(KIND_DOC)

    if len(parts) >= 2:
        container = parts[1]
        for directory, suffix, kind in (
            ("symbols", lb.SYMDIR_SUFFIX, KIND_SYMBOL),
            ("footprints", lb.PRETTY_SUFFIX, KIND_FOOTPRINT),
            ("3dmodels", lb.SHAPES_SUFFIX, KIND_MODEL),
        ):
            if head == directory and container.endswith(suffix):
                return PathInfo(kind, container[: -len(suffix)])
    return PathInfo(KIND_OTHER)


def _count_phrase(count: int, kind: str) -> str:
    noun = _PLURALS.get(kind, kind + "s")
    if count == 1:
        singular = {KIND_MODEL: "3D model", KIND_TEMPLATE: "template file"}.get(kind, kind)
        return f"1 {singular}"
    return f"{count} {noun}"


def _group(paths: Sequence[str]) -> Dict[Tuple[str, str], int]:
    out: Dict[Tuple[str, str], int] = {}
    for path in paths:
        info = classify_path(path)
        key = (info.kind, info.category)
        out[key] = out.get(key, 0) + 1
    return out


def _content_clauses(verb: str, preposition: str, paths: Sequence[str]) -> List[str]:
    """`Add 3 symbols to Conn_XT`, and the multi-category form."""
    grouped = _group(paths)
    clauses: List[str] = []
    for kind in (KIND_SYMBOL, KIND_FOOTPRINT, KIND_MODEL, KIND_TEMPLATE):
        categories = {cat: n for (k, cat), n in grouped.items() if k == kind}
        if not categories:
            continue
        total = sum(categories.values())
        phrase = _count_phrase(total, kind)
        named = [c for c in categories if c]
        if len(named) == 1 and len(categories) == 1:
            clauses.append(f"{verb} {phrase} {preposition} {named[0]}")
        elif len(categories) > 1:
            clauses.append(f"{verb} {phrase} {preposition} {len(categories)} categories")
        else:
            clauses.append(f"{verb} {phrase}")
    return clauses


def suggest_commit_message(
    root: Path,
    status: Optional[RepoStatus] = None,
    lib: Optional[lb.Library] = None,
) -> str:
    """
    A commit message describing what actually changed.

    "Update files" is worse than nothing on a repository synced between two
    machines: six months later the history is the only record of when a part
    arrived. This reads the status and says so. It is always editable in the
    UI -- a convenience, not a policy.

    `lib` is accepted for callers that already hold a scan; the
    classification deliberately does not need it (see `classify_path`).
    """
    del lib  # see the docstring
    status = status if status is not None else read_status(root)
    if not status.dirty:
        return ""

    added = list(status.untracked) + [e.path for e in status.entries
                                      if e.letter == "A" and not e.untracked]
    removed = list(status.deleted) + [e.path for e in status.entries if e.letter == "D"]
    renamed = [e for e in status.entries if e.letter == "R"]
    touched = [e.path for e in status.entries
               if e.letter == "M" and e.path not in added]

    # De-duplicate while keeping order; a path can appear in both the index
    # and the working-tree column.
    def unique(values: Sequence[str]) -> List[str]:
        seen, out = set(), []
        for value in values:
            if value not in seen:
                seen.add(value)
                out.append(value)
        return out

    added, removed, touched = unique(added), unique(removed), unique(touched)

    clauses: List[str] = []
    clauses += _content_clauses("Add", "to", added)
    clauses += _content_clauses("Remove", "from", removed)
    clauses += _content_clauses("Update", "in", touched)
    for entry in renamed:
        clauses.append(f"Rename {Path(entry.orig_path).name or entry.orig_path} "
                       f"to {Path(entry.path).name}")

    everything = added + removed + touched + [e.path for e in renamed]
    kinds = {classify_path(p).kind for p in everything}
    if KIND_TABLE in kinds:
        clauses.append("Regenerate master library tables")
    if KIND_PROVENANCE in kinds:
        clauses.append("Update provenance")
    for kind, label in ((KIND_SCRIPT, "Update scripts"),
                        (KIND_TEST, "Update tests"),
                        (KIND_DOC, "Update documentation")):
        if kind in kinds:
            clauses.append(label)
    if KIND_OTHER in kinds and not clauses:
        clauses.append(f"Update {len(everything)} file(s)")

    if not clauses:
        return f"Update {len(everything)} file(s)"
    if len(clauses) == 1:
        return clauses[0]

    subject = "Update library: " + ", ".join(
        c[0].lower() + c[1:] for c in clauses[:2])
    if len(clauses) > 2 or len(subject) > 72:
        subject = f"Update library ({len(everything)} files)"
    body = "\n".join(f"- {clause}" for clause in clauses)
    return f"{subject}\n\n{body}"


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------

def _paths_to_stage(status: RepoStatus) -> List[str]:
    """
    Exactly the paths the preview listed, so the commit holds no surprises.

    A bare `git add -A` would also pick up anything created since the
    preview was shown, which on a library this size is a real possibility
    while a dialog sits open.
    """
    seen, out = set(), []
    for entry in status.entries:
        for path in (entry.path, entry.orig_path):
            if path and path not in seen:
                seen.add(path)
                out.append(path)
    return out


def preview_commit(
    root: Path,
    status: Optional[RepoStatus] = None,
    lib: Optional[lb.Library] = None,
) -> CommitPreview:
    """What committing right now would record, and anything worth saying first."""
    status = status if status is not None else read_status(root)
    preview = CommitPreview()
    if not status.is_repo:
        preview.warnings.append("This folder is not a git repository.")
        return preview

    preview.files = [(e.letter, e.path) for e in status.entries]
    preview.message = suggest_commit_message(root, status, lib)

    if status.unmerged:
        preview.warnings.append(
            f"{len(status.unmerged)} file(s) have unresolved conflicts and must be "
            f"resolved in a terminal before committing."
        )
    if status.deleted:
        preview.warnings.append(
            f"{len(status.deleted)} file(s) are being removed. Any project still "
            f"referencing them will fail to load the part."
        )
    for letter, path in preview.files:
        if letter == "D":
            continue
        try:
            size = (root / path).stat().st_size
        except OSError:
            continue
        if size > LARGE_FILE_WARN_BYTES:
            preview.warnings.append(
                f"{path} is {size / (1024 * 1024):.1f} MB. Git keeps every version "
                f"of a binary file in full, in every clone."
            )
    return preview


def commit(root: Path, message: str) -> GitResult:
    """
    Stage everything the status reported, then commit it.

    Untracked files are included deliberately: a newly imported part is
    untracked, and a commit that silently left it out would be the single
    most confusing thing this feature could do.
    """
    message = (message or "").strip()
    if not message:
        return GitResult(("commit",), 1, "", "A commit needs a message.")

    status = read_status(root)
    if not status.is_repo:
        return GitResult(("commit",), 1, "", "This folder is not a git repository.")
    blocked = status.blockers().get(COMMIT)
    if blocked:
        return GitResult(("commit",), 1, "", blocked)

    paths = _paths_to_stage(status)
    transcript: List[str] = []
    # Chunked because a reorganised library can touch thousands of paths and
    # Windows caps a command line at 32 KiB.
    for start in range(0, len(paths), 100):
        staged = _run(root, "add", "--all", "--", *paths[start:start + 100])
        transcript.append(staged.transcript())
        if not staged.ok:
            return GitResult(staged.command, staged.returncode,
                             "\n".join(transcript), staged.stderr)

    done = _run(root, "commit", "-m", message)
    transcript.append(done.transcript())
    return GitResult(done.command, done.returncode,
                     "\n".join(transcript), done.stderr)


def fetch(root: Path) -> GitResult:
    """Update the remote-tracking refs. Changes nothing in the working tree."""
    status = read_status(root)
    blocked = status.blockers().get(FETCH)
    if blocked:
        return GitResult(("fetch",), 1, "", blocked)
    return _run(root, "fetch", "--no-tags", str(status.remote_name),
                timeout=NETWORK_TIMEOUT_S)


def pull_ff_only(root: Path) -> GitResult:
    """
    Fast-forward to upstream, or refuse.

    `--ff-only` is the whole point: it can only advance the branch to a
    commit that already contains the local one, so it cannot create a merge,
    cannot conflict, and cannot lose a local commit. Anything that would need
    more than that is reported and left for a terminal.
    """
    status = read_status(root)
    blocked = status.blockers().get(PULL)
    if blocked:
        return GitResult(("pull", "--ff-only"), 1, "", blocked)
    return _run(root, "pull", "--ff-only", timeout=NETWORK_TIMEOUT_S)


def push(root: Path) -> GitResult:
    """
    Send the outgoing commits upstream.

    Never forced. A rejected push means the remote has something this clone
    does not, and resolving that by force is how the other machine's work
    disappears.
    """
    status = read_status(root)
    blocked = status.blockers().get(PUSH)
    if blocked:
        return GitResult(("push",), 1, "", blocked)
    return _run(root, "push", timeout=NETWORK_TIMEOUT_S)
