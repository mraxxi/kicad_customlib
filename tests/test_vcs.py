"""
Phase 3: core/vcs.py.

Every test runs against a throwaway repository with a `git init --bare`
"remote" on the same filesystem, so the whole file is headless, offline and
deterministic. The one test that deliberately touches a URL uses an
unroutable address with a short timeout, and asserts it fails fast rather
than hanging -- which is the actual thing being verified.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.core import vcs

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git is not installed")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def git(root: Path, *args: str) -> str:
    """Run git in `root` for test setup, loudly."""
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "LC_ALL": "C",
    })
    proc = subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"git {' '.join(args)}\n{proc.stdout}{proc.stderr}"
    return proc.stdout


def write(root: Path, rel: str, content: str = "x\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    """A local stand-in for origin."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   capture_output=True, check=True)
    return remote


@pytest.fixture
def repo(tmp_path: Path, bare: Path) -> Path:
    """A clone with one commit, tracking `main`, clean and in sync."""
    root = tmp_path / "work"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "remote", "add", "origin", str(bare))
    write(root, "README.md", "library\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", "Initial commit")
    git(root, "push", "-u", "origin", "main")
    return root


@pytest.fixture
def second(tmp_path: Path, bare: Path, repo: Path) -> Path:
    """
    A second clone of the same remote -- the other machine.

    This is what makes `behind` and `diverged` reachable without a network.
    """
    root = tmp_path / "other"
    subprocess.run(["git", "clone", str(bare), str(root)],
                   capture_output=True, check=True)
    git(root, "config", "user.name", "Other")
    git(root, "config", "user.email", "other@example.com")
    git(root, "config", "commit.gpgsign", "false")
    return root


def commit_in(root: Path, rel: str, message: str) -> None:
    write(root, rel, f"{message}\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", message)


# --------------------------------------------------------------------------
# Not a repository
# --------------------------------------------------------------------------

def test_a_plain_directory_is_not_an_error(tmp_path):
    """
    The library is perfectly usable without git, so this is a normal answer
    and the interface simply hides the sync controls.
    """
    status = vcs.read_status(tmp_path)
    assert status.is_repo is False
    assert status.state == vcs.NOT_A_REPO
    assert status.summary_line() == ""


def test_every_action_is_blocked_outside_a_repo(tmp_path):
    blockers = vcs.read_status(tmp_path).blockers()
    assert set(blockers) == {vcs.FETCH, vcs.PULL, vcs.COMMIT, vcs.PUSH}
    assert all(reason.endswith(".") for reason in blockers.values())


def test_operations_outside_a_repo_report_rather_than_raise(tmp_path):
    for result in (vcs.fetch(tmp_path), vcs.pull_ff_only(tmp_path),
                   vcs.push(tmp_path), vcs.commit(tmp_path, "msg")):
        assert not result.ok
        assert "not a git repository" in result.stderr


# --------------------------------------------------------------------------
# States, in the documented precedence
# --------------------------------------------------------------------------

def test_a_fresh_clone_is_in_sync(repo):
    status = vcs.read_status(repo)
    assert status.is_repo
    assert status.state == vcs.IN_SYNC
    assert status.branch == "main"
    assert status.upstream == "origin/main"
    assert status.remote_name == "origin"
    assert status.remote_url
    assert (status.ahead, status.behind) == (0, 0)
    assert status.dirty is False
    assert status.head and status.head.subject == "Initial commit"


def test_ahead(repo):
    commit_in(repo, "symbols/A.kicad_symdir/X.kicad_sym", "local work")
    status = vcs.read_status(repo)
    assert status.state == vcs.AHEAD
    assert (status.ahead, status.behind) == (1, 0)


def test_behind(repo, second):
    commit_in(second, "remote.txt", "remote work")
    git(second, "push")
    vcs.fetch(repo)
    status = vcs.read_status(repo)
    assert status.state == vcs.BEHIND
    assert (status.ahead, status.behind) == (0, 1)


def test_diverged(repo, second):
    commit_in(second, "remote.txt", "remote work")
    git(second, "push")
    commit_in(repo, "local.txt", "local work")
    vcs.fetch(repo)
    status = vcs.read_status(repo)
    assert status.state == vcs.DIVERGED
    assert status.ahead == 1 and status.behind == 1


@pytest.mark.parametrize("make,field", [
    (lambda r: write(r, "README.md", "edited\n"), "modified"),
    (lambda r: (r / "README.md").unlink(), "deleted"),
    (lambda r: write(r, "brand-new.txt"), "untracked"),
])
def test_dirty_in_each_flavour(repo, make, field):
    make(repo)
    status = vcs.read_status(repo)
    assert status.state == vcs.DIRTY
    assert status.dirty is True
    assert getattr(status, field) == ["README.md" if field != "untracked"
                                      else "brand-new.txt"]


def test_a_staged_change_is_reported_as_staged(repo):
    write(repo, "README.md", "edited\n")
    git(repo, "add", "README.md")
    status = vcs.read_status(repo)
    assert status.staged == ["README.md"]
    assert status.modified == []
    assert status.state == vcs.DIRTY


def test_detached_head(repo):
    commit_in(repo, "a.txt", "second")
    git(repo, "checkout", "--detach", "HEAD~1")
    status = vcs.read_status(repo)
    assert status.detached is True
    assert status.branch is None
    assert status.state == vcs.DETACHED
    assert "detached" in status.summary_line()


def test_no_upstream(tmp_path):
    root = tmp_path / "solo"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "commit.gpgsign", "false")
    commit_in(root, "a.txt", "only commit")
    status = vcs.read_status(root)
    assert status.upstream is None
    assert status.state == vcs.NO_UPSTREAM
    assert "no upstream" in status.summary_line()


def test_unmerged_outranks_dirty_and_ahead(repo, second):
    """A conflict must be the reported state, whatever else is also true."""
    commit_in(second, "shared.txt", "theirs")
    git(second, "push")
    commit_in(repo, "shared.txt", "ours")
    vcs.fetch(repo)
    merge = subprocess.run(["git", "-C", str(repo), "merge", "origin/main"],
                           capture_output=True, text=True)
    assert merge.returncode != 0, "expected the merge to conflict"
    status = vcs.read_status(repo)
    assert status.unmerged == ["shared.txt"]
    # A conflicted merge leaves MERGE_HEAD, so operation_in_progress wins --
    # which is the documented precedence.
    assert status.state == vcs.OPERATION_IN_PROGRESS
    assert status.operation == "merge"


def test_operation_in_progress_is_detected_from_the_git_dir(repo):
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
    status = vcs.read_status(repo)
    assert status.operation == "merge"
    assert status.state == vcs.OPERATION_IN_PROGRESS
    (repo / ".git" / "MERGE_HEAD").unlink()
    (repo / ".git" / "rebase-merge").mkdir()
    assert vcs.read_status(repo).operation == "rebase"


def test_state_precedence_is_the_documented_order():
    """Checked directly, because several states are true at once in practice."""
    base = dict(is_repo=True, branch="main", upstream="origin/main")
    everything = dict(base, operation="merge", unmerged=["a"], detached=True,
                      ahead=1, behind=1, modified=["b"])
    assert vcs.RepoStatus(**everything).state == vcs.OPERATION_IN_PROGRESS
    everything.pop("operation")
    assert vcs.RepoStatus(**everything).state == vcs.UNMERGED
    everything.pop("unmerged")
    assert vcs.RepoStatus(**everything).state == vcs.DETACHED
    everything.pop("detached")
    assert vcs.RepoStatus(**everything).state == vcs.DIVERGED
    everything["ahead"] = 0
    assert vcs.RepoStatus(**everything).state == vcs.BEHIND
    everything.update(ahead=1, behind=0)
    assert vcs.RepoStatus(**everything).state == vcs.AHEAD
    everything["ahead"] = 0
    assert vcs.RepoStatus(**everything).state == vcs.DIRTY
    everything["modified"] = []
    assert vcs.RepoStatus(**everything).state == vcs.IN_SYNC
    everything["upstream"] = None
    assert vcs.RepoStatus(**everything).state == vcs.NO_UPSTREAM


# --------------------------------------------------------------------------
# Blockers
# --------------------------------------------------------------------------

def test_in_sync_allows_only_fetch(repo):
    blockers = vcs.read_status(repo).blockers()
    assert vcs.FETCH not in blockers
    assert set(blockers) == {vcs.PULL, vcs.COMMIT, vcs.PUSH}
    assert blockers[vcs.PULL] == "There is nothing to pull."
    assert blockers[vcs.COMMIT] == "There is nothing to commit."
    assert blockers[vcs.PUSH] == "There is nothing to push."


def test_a_dirty_tree_allows_commit_and_blocks_push(repo):
    commit_in(repo, "a.txt", "local")
    write(repo, "b.txt")
    blockers = vcs.read_status(repo).blockers()
    assert vcs.COMMIT not in blockers
    assert "uncommitted" in blockers[vcs.PUSH]


def test_a_dirty_tree_blocks_a_pull_with_a_useful_reason(repo, second):
    """
    git's own message here talks about overwriting local changes, which does
    not tell the user what to do. Refusing early does.
    """
    commit_in(second, "remote.txt", "remote")
    git(second, "push")
    vcs.fetch(repo)
    write(repo, "README.md", "edited\n")
    blockers = vcs.read_status(repo).blockers()
    assert "Commit them first" in blockers[vcs.PULL]


def test_diverged_blocks_both_pull_and_push_mentioning_a_terminal(repo, second):
    commit_in(second, "remote.txt", "remote")
    git(second, "push")
    commit_in(repo, "local.txt", "local")
    vcs.fetch(repo)
    blockers = vcs.read_status(repo).blockers()
    assert "diverged" in blockers[vcs.PULL]
    assert "terminal" in blockers[vcs.PULL]
    assert "diverged" in blockers[vcs.PUSH]


def test_no_upstream_blocks_pull_and_push_but_not_fetch(repo):
    git(repo, "checkout", "-b", "side")
    blockers = vcs.read_status(repo).blockers()
    assert vcs.FETCH not in blockers
    assert "no upstream" in blockers[vcs.PULL]
    assert "git push -u" in blockers[vcs.PUSH]


def test_no_remote_blocks_fetch(tmp_path):
    root = tmp_path / "solo"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "commit.gpgsign", "false")
    commit_in(root, "a.txt", "only")
    assert "no remote" in vcs.read_status(root).blockers()[vcs.FETCH]


def test_an_operation_in_progress_blocks_everything_but_fetch(repo):
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
    blockers = vcs.read_status(repo).blockers()
    assert vcs.FETCH not in blockers
    for action in (vcs.PULL, vcs.COMMIT, vcs.PUSH):
        assert "merge is in progress" in blockers[action]


def test_every_blocker_reason_is_a_sentence():
    """They are shown to the user verbatim, so they must read as prose."""
    cases = [
        vcs.RepoStatus(),
        vcs.RepoStatus(is_repo=True, branch="main", upstream="origin/main",
                       remote_name="origin"),
        vcs.RepoStatus(is_repo=True, branch="main", upstream="origin/main",
                       remote_name="origin", ahead=1, behind=1, modified=["a"]),
        vcs.RepoStatus(is_repo=True, detached=True, remote_name="origin"),
        vcs.RepoStatus(is_repo=True, branch="main", operation="rebase",
                       remote_name="origin"),
        vcs.RepoStatus(is_repo=True, branch="main", upstream="origin/main",
                       remote_name="origin", unmerged=["a"]),
    ]
    for status in cases:
        for action, reason in status.blockers().items():
            # A count may legitimately open the sentence ("3 file(s) ...").
            assert reason and (reason[0].isupper() or reason[0].isdigit()), \
                (status.state, action, reason)
            assert reason.rstrip().endswith("."), (status.state, action, reason)


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------

def test_commit_includes_untracked_files(repo):
    """
    An imported part is untracked. A commit that quietly left it out would be
    the most confusing thing this feature could do.
    """
    write(repo, "symbols/Conn_XT.kicad_symdir/XT60.kicad_sym", "(symbol)\n")
    result = vcs.commit(repo, "Add XT60")
    assert result.ok, result.transcript()
    status = vcs.read_status(repo)
    assert status.dirty is False
    assert status.ahead == 1
    assert "XT60" in git(repo, "show", "--name-only", "--format=", "HEAD")


def test_commit_refuses_an_empty_message(repo):
    write(repo, "a.txt")
    assert not vcs.commit(repo, "   ").ok
    assert vcs.read_status(repo).dirty is True


def test_commit_refuses_when_there_is_nothing_to_commit(repo):
    result = vcs.commit(repo, "Nothing")
    assert not result.ok
    assert "nothing to commit" in result.stderr


def test_commit_refuses_mid_merge(repo):
    write(repo, "a.txt")
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
    result = vcs.commit(repo, "During a merge")
    assert not result.ok
    assert "merge is in progress" in result.stderr


def test_push_sends_the_outgoing_commits(repo, bare):
    commit_in(repo, "a.txt", "first local")
    commit_in(repo, "b.txt", "second local")
    assert [c.subject for c in vcs.outgoing(repo)] == ["second local", "first local"]
    result = vcs.push(repo)
    assert result.ok, result.transcript()
    assert vcs.read_status(repo).ahead == 0
    assert vcs.outgoing(repo) == []


def test_pull_fast_forwards(repo, second):
    commit_in(second, "remote.txt", "from the other machine")
    git(second, "push")
    vcs.fetch(repo)
    assert [c.subject for c in vcs.incoming(repo)] == ["from the other machine"]
    result = vcs.pull_ff_only(repo)
    assert result.ok, result.transcript()
    status = vcs.read_status(repo)
    assert status.state == vcs.IN_SYNC
    assert (repo / "remote.txt").exists()


def test_pull_refuses_to_merge_when_diverged(repo, second):
    """The refusal is the feature: a merge commit here needs a human."""
    commit_in(second, "remote.txt", "theirs")
    git(second, "push")
    commit_in(repo, "local.txt", "ours")
    vcs.fetch(repo)
    before = git(repo, "rev-parse", "HEAD").strip()
    result = vcs.pull_ff_only(repo)
    assert not result.ok
    assert "diverged" in result.stderr
    assert git(repo, "rev-parse", "HEAD").strip() == before
    assert vcs.read_status(repo).operation is None


def test_fetch_updates_the_tracking_ref_and_nothing_else(repo, second):
    commit_in(second, "remote.txt", "theirs")
    git(second, "push")
    assert vcs.read_status(repo).behind == 0        # not yet known
    result = vcs.fetch(repo)
    assert result.ok, result.transcript()
    status = vcs.read_status(repo)
    assert status.behind == 1
    assert not (repo / "remote.txt").exists()
    assert status.last_fetch is not None


def test_incoming_and_outgoing_are_empty_without_an_upstream(repo):
    git(repo, "checkout", "-b", "side")
    assert vcs.incoming(repo) == []
    assert vcs.outgoing(repo) == []


# --------------------------------------------------------------------------
# Awkward paths
# --------------------------------------------------------------------------

def test_a_path_with_a_space_and_non_ascii_survives_parsing(repo):
    """
    Why `-z`: the default porcelain format quotes and escapes such a path,
    and this library is full of vendor names that need it.
    """
    name = "3dmodels/Conn Tèst.3dshapes/PJ-35 320 Série.step"
    write(repo, name, "solid\n")
    status = vcs.read_status(repo)
    assert status.untracked == [name]
    assert vcs.commit(repo, "Add a part with an awkward name").ok
    assert vcs.read_status(repo).dirty is False


def test_a_rename_keeps_both_names(repo):
    commit_in(repo, "footprints/C.pretty/Old.kicad_mod", "fp")
    git(repo, "mv", "footprints/C.pretty/Old.kicad_mod",
        "footprints/C.pretty/New.kicad_mod")
    status = vcs.read_status(repo)
    entry = [e for e in status.entries if e.letter == "R"]
    assert len(entry) == 1
    assert entry[0].path.endswith("New.kicad_mod")
    assert entry[0].orig_path.endswith("Old.kicad_mod")


def test_status_parsing_of_a_handcrafted_payload():
    payload = ("1 M README.md\0"
               "R  footprints/C.pretty/New.kicad_mod\0footprints/C.pretty/Old.kicad_mod\0"
               "?? brand new.txt\0"
               "UU shared.txt\0").replace("1 M", " M")
    entries = vcs._parse_status_z(payload)
    assert [(e.x, e.y, e.path) for e in entries] == [
        (" ", "M", "README.md"),
        ("R", " ", "footprints/C.pretty/New.kicad_mod"),
        ("?", "?", "brand new.txt"),
        ("U", "U", "shared.txt"),
    ]
    assert entries[1].orig_path == "footprints/C.pretty/Old.kicad_mod"
    assert entries[3].unmerged is True
    assert entries[2].untracked is True


# --------------------------------------------------------------------------
# Hardening
# --------------------------------------------------------------------------

def test_the_environment_cannot_ask_a_human_anything():
    env = vcs._env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == ""
    assert env["SSH_ASKPASS"] == ""
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    assert "DISPLAY" not in env and "WAYLAND_DISPLAY" not in env
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["LC_ALL"] == "C"


def test_an_unreachable_remote_always_comes_back(repo, monkeypatch):
    """
    The failure this guards against is an operation that never returns: a
    remote needing a password, or one that simply does not answer, otherwise
    blocks the caller forever, because the credential helper waits on a
    terminal the GUI does not have.

    192.0.2.1 is TEST-NET-1, which is black-holed rather than refused, so the
    connection cannot fail quickly -- which makes it the right address for
    checking that the timeout, not the network, is what ends the call.
    """
    git(repo, "remote", "set-url", "origin", "https://192.0.2.1/nope.git")
    monkeypatch.setattr(vcs, "NETWORK_TIMEOUT_S", 5)
    started = datetime.now()
    result = vcs.fetch(repo)
    elapsed = (datetime.now() - started).total_seconds()
    assert not result.ok
    assert result.message, "a failure must say something"
    # Generous, because a slow runner must not make this flaky; the point is
    # that it is bounded at all.
    assert elapsed < 30, "fetch never returned"


def test_a_timeout_returns_a_result_rather_than_raising(repo):
    result = vcs._run(repo, "log", "-1", timeout=0)
    assert result.returncode == vcs.EXIT_TIMEOUT
    assert "did not finish" in result.stderr
    assert "Nothing was changed" in result.stderr


def test_a_missing_git_binary_is_reported_not_raised(repo, monkeypatch):
    def boom(*a, **k):
        raise OSError("No such file or directory: 'git'")
    monkeypatch.setattr(vcs.subprocess, "run", boom)
    result = vcs._run(repo, "status")
    assert result.returncode == vcs.EXIT_UNAVAILABLE
    assert "could not be run" in result.stderr
    assert vcs.read_status(repo).is_repo is False


FORBIDDEN = ("reset", "checkout", "stash", "rebase", "merge", "clean", "gc",
             "filter-branch", "prune", "update-ref", "branch", "switch",
             "restore", "rm", "mv", "cherry-pick", "revert")

ALLOWED = {"--version", "rev-parse", "symbolic-ref", "status", "log", "rev-list",
           "remote", "config", "add", "commit", "fetch", "pull", "push"}


def test_no_destructive_git_subcommand_appears_in_the_module():
    """
    The guard rail, checked mechanically.

    Everything goes through `_run(root, "<subcommand>", ...)`, so the set of
    subcommands this module can issue is readable straight off the source.
    Anything in FORBIDDEN can discard work that exists in no other clone, and
    on a two-machine library that work is irreplaceable.
    """
    source = (Path(vcs.__file__)).read_text(encoding="utf-8")
    used = set(re.findall(r'_run\(\s*root,\s*"([a-z][a-z-]*)"', source))
    used |= set(re.findall(r'_run\(\s*Path\.cwd\(\),\s*"(-?-?[a-z][a-z-]*)"', source))
    assert used <= ALLOWED, f"unexpected git subcommand(s): {sorted(used - ALLOWED)}"
    assert not used & set(FORBIDDEN)


def test_the_module_never_forces_a_push():
    source = (Path(vcs.__file__)).read_text(encoding="utf-8")
    calls = re.findall(r'_run\((.*?)\)\n', source, flags=re.S)
    for call in calls:
        assert "--force" not in call
        assert '"-f"' not in call


# --------------------------------------------------------------------------
# Commit messages
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path,kind,category", [
    ("symbols/Conn_XT.kicad_symdir/XT60.kicad_sym", vcs.KIND_SYMBOL, "Conn_XT"),
    ("footprints/Conn_XT.pretty/XT60.kicad_mod", vcs.KIND_FOOTPRINT, "Conn_XT"),
    ("3dmodels/Conn_XT.3dshapes/XT60.step", vcs.KIND_MODEL, "Conn_XT"),
    ("sym-lib-table", vcs.KIND_TABLE, ""),
    ("fp-lib-table", vcs.KIND_TABLE, ""),
    ("provenance.json", vcs.KIND_PROVENANCE, ""),
    ("template/amp.kicad_wks", vcs.KIND_TEMPLATE, ""),
    ("scripts/lib_manager.py", vcs.KIND_SCRIPT, ""),
    ("tests/test_vcs.py", vcs.KIND_TEST, ""),
    ("README.md", vcs.KIND_DOC, ""),
    ("staging-temp/intake/x", vcs.KIND_OTHER, ""),
    ("symbols/loose.kicad_sym", vcs.KIND_OTHER, ""),
])
def test_path_classification(path, kind, category):
    info = vcs.classify_path(path)
    assert (info.kind, info.category) == (kind, category)


def test_message_for_new_symbols_names_the_category(repo):
    write(repo, "symbols/Conn_XT.kicad_symdir/XT60.kicad_sym")
    write(repo, "symbols/Conn_XT.kicad_symdir/XT90.kicad_sym")
    assert vcs.suggest_commit_message(repo) == "Add 2 symbols to Conn_XT"


def test_message_for_one_symbol_is_singular(repo):
    write(repo, "symbols/Conn_XT.kicad_symdir/XT60.kicad_sym")
    assert vcs.suggest_commit_message(repo) == "Add 1 symbol to Conn_XT"


def test_message_for_regenerated_tables_only(repo):
    write(repo, "sym-lib-table", "(sym_lib_table)\n")
    write(repo, "fp-lib-table", "(fp_lib_table)\n")
    assert vcs.suggest_commit_message(repo) == "Regenerate master library tables"


def test_message_for_provenance_only(repo):
    write(repo, "provenance.json", "{}\n")
    assert vcs.suggest_commit_message(repo) == "Update provenance"


def test_message_for_a_deletion_says_remove(repo):
    commit_in(repo, "footprints/C.pretty/Gone.kicad_mod", "fp")
    (repo / "footprints" / "C.pretty" / "Gone.kicad_mod").unlink()
    assert vcs.suggest_commit_message(repo) == "Remove 1 footprint from C"


def test_message_spanning_categories_counts_them(repo):
    write(repo, "symbols/A.kicad_symdir/One.kicad_sym")
    write(repo, "symbols/B.kicad_symdir/Two.kicad_sym")
    assert vcs.suggest_commit_message(repo) == "Add 2 symbols to 2 categories"


def test_a_mixed_change_gets_a_subject_and_a_bulleted_body(repo):
    write(repo, "symbols/Conn_XT.kicad_symdir/XT60.kicad_sym")
    write(repo, "footprints/Conn_XT.pretty/XT60.kicad_mod")
    write(repo, "sym-lib-table", "(sym_lib_table)\n")
    write(repo, "provenance.json", "{}\n")
    message = vcs.suggest_commit_message(repo)
    subject, _blank, body = message.split("\n", 2)
    assert len(subject) <= 72
    assert subject.startswith("Update library")
    assert "- Add 1 symbol to Conn_XT" in body
    assert "- Add 1 footprint to Conn_XT" in body
    assert "- Regenerate master library tables" in body
    assert "- Update provenance" in body


def test_no_message_for_a_clean_tree(repo):
    assert vcs.suggest_commit_message(repo) == ""


# --------------------------------------------------------------------------
# Commit preview
# --------------------------------------------------------------------------

def test_preview_lists_the_status_letters(repo):
    write(repo, "README.md", "edited\n")
    write(repo, "new.txt")
    preview = vcs.preview_commit(repo)
    assert sorted(preview.files) == [("?", "new.txt"), ("M", "README.md")]
    assert preview.message
    assert preview.is_empty is False


def test_preview_warns_about_deletions_breaking_projects(repo):
    commit_in(repo, "footprints/C.pretty/Gone.kicad_mod", "fp")
    (repo / "footprints" / "C.pretty" / "Gone.kicad_mod").unlink()
    warnings = " ".join(vcs.preview_commit(repo).warnings)
    assert "referencing them will fail" in warnings


def test_preview_warns_about_a_large_binary(repo, monkeypatch):
    monkeypatch.setattr(vcs, "LARGE_FILE_WARN_BYTES", 1024)
    write(repo, "3dmodels/C.3dshapes/Big.step", "x" * 4096)
    warnings = " ".join(vcs.preview_commit(repo).warnings)
    assert "every version of a binary file" in warnings


def test_preview_warns_about_unresolved_conflicts(repo):
    write(repo, "a.txt")
    status = vcs.read_status(repo)
    status.unmerged = ["shared.txt"]
    assert "resolved in a terminal" in " ".join(
        vcs.preview_commit(repo, status).warnings)


def test_preview_of_a_clean_tree_is_empty(repo):
    preview = vcs.preview_commit(repo)
    assert preview.is_empty
    assert preview.summary() == "Nothing to commit."


def test_a_preview_writes_nothing(repo):
    """Same discipline as ops.Plan, even though this is not one."""
    write(repo, "a.txt")
    before = git(repo, "status", "--porcelain")
    head = git(repo, "rev-parse", "HEAD")
    vcs.preview_commit(repo)
    vcs.suggest_commit_message(repo)
    assert git(repo, "status", "--porcelain") == before
    assert git(repo, "rev-parse", "HEAD") == head


def test_a_commit_holds_only_what_the_preview_listed(repo):
    """
    A bare `git add -A` would also sweep up anything created while the dialog
    sat open, which on a library this size is a real possibility.
    """
    write(repo, "planned.txt")
    status = vcs.read_status(repo)
    write(repo, "appeared-later.txt")
    for start in range(0, len(vcs._paths_to_stage(status)), 100):
        pass
    assert vcs._paths_to_stage(status) == ["planned.txt"]


# --------------------------------------------------------------------------
# Presentation helpers
# --------------------------------------------------------------------------

def test_fetch_age_wording():
    now = datetime(2026, 10, 5, 12, 0, 0)
    assert vcs.describe_fetch_age(None) == "never fetched"
    assert vcs.describe_fetch_age(now - timedelta(seconds=5), now) == "fetched just now"
    assert vcs.describe_fetch_age(now - timedelta(minutes=4), now) == "fetched 4 min ago"
    assert vcs.describe_fetch_age(now - timedelta(hours=1), now) == "fetched 1 hour ago"
    assert vcs.describe_fetch_age(now - timedelta(hours=5), now) == "fetched 5 hours ago"
    assert vcs.describe_fetch_age(now - timedelta(days=1), now) == "fetched 1 day ago"
    assert vcs.describe_fetch_age(now - timedelta(days=9), now) == "fetched 9 days ago"


def test_never_fetched_is_said_outright(repo):
    """
    Ahead/behind is computed against the tracking ref, which only a fetch
    updates. "In sync" after a week without one means nothing, so the age is
    part of the indicator, not decoration.
    """
    fetch_head = repo / ".git" / "FETCH_HEAD"
    if fetch_head.exists():
        fetch_head.unlink()
    assert "never fetched" in vcs.read_status(repo).summary_line()


def test_summary_line_shape(repo):
    commit_in(repo, "a.txt", "local")
    write(repo, "b.txt")
    line = vcs.read_status(repo).summary_line()
    assert line.startswith("main · ")
    assert "↑1 ↓0" in line
    assert "1 changed" in line


def test_git_result_message_prefers_stderr():
    result = vcs.GitResult(("push",), 1, "some output\n", "fatal: nope\n")
    assert result.message == "fatal: nope"
    assert "$ git push" in result.transcript()
    assert "(exit 1)" in result.transcript()


def test_git_result_ok_is_the_exit_code():
    assert vcs.GitResult(("log",), 0).ok is True
    assert vcs.GitResult(("log",), 1).ok is False


# --------------------------------------------------------------------------
# CLI parity
# --------------------------------------------------------------------------

@pytest.fixture
def sync_cli(monkeypatch, repo, capsys):
    """
    Run `lib_manager.py sync ...` against the throwaway repository.

    The audit is stubbed to a clean report in most cases: what is being
    tested here is the command's own plumbing, and `tests/test_check.py`
    already owns the audit's behaviour.
    """
    import lib_manager

    monkeypatch.setattr(lib_manager, "ROOT_DIR", repo)
    monkeypatch.setattr(lib_manager.sys.stdin, "isatty", lambda: False, raising=False)

    def run(*argv):
        code = lib_manager.main(["sync", *argv])
        return code, capsys.readouterr()

    run.repo = repo
    run.module = lib_manager
    return run


def _clean_report(monkeypatch, repo, *, errors=0):
    import lib_manager
    from src.core import check as check_mod

    report = check_mod.Report(root=repo)
    for i in range(errors):
        report.add(check_mod.ERROR, "test.broken", f"synthetic error {i + 1}")
    monkeypatch.setattr(lib_manager.check_mod, "run", lambda *a, **k: report)
    return report


def test_cli_sync_status(sync_cli):
    code, captured = sync_cli("status")
    assert code == 0
    assert "State:     in_sync" in captured.out
    assert "Upstream:  origin/main" in captured.out
    assert "Tree:      clean" in captured.out


def test_cli_sync_with_no_subcommand_shows_the_status(sync_cli):
    code, captured = sync_cli()
    assert code == 0
    assert "State:" in captured.out


def test_cli_sync_status_json_is_machine_readable(sync_cli):
    import json as _json
    code, captured = sync_cli("status", "--json")
    assert code == 0
    payload = _json.loads(captured.out)
    assert payload["state"] == "in_sync"
    assert payload["branch"] == "main"
    assert payload["dirty"] is False
    assert payload["blockers"]["commit"] == "There is nothing to commit."


def test_cli_sync_status_outside_a_repo(monkeypatch, tmp_path, capsys):
    import lib_manager
    monkeypatch.setattr(lib_manager, "ROOT_DIR", tmp_path)
    assert lib_manager.main(["sync", "status"]) == 0
    assert "Not a git repository." in capsys.readouterr().out


def test_cli_sync_status_lists_incoming_and_outgoing(sync_cli, second):
    commit_in(second, "theirs.txt", "from the other machine")
    git(second, "push")
    commit_in(sync_cli.repo, "ours.txt", "from this machine")
    vcs.fetch(sync_cli.repo)
    code, captured = sync_cli("status")
    assert code == 0
    assert "from the other machine" in captured.out
    assert "from this machine" in captured.out


def test_cli_sync_commit_dry_run_changes_nothing(sync_cli):
    write(sync_cli.repo, "symbols/Conn_XT.kicad_symdir/XT60.kicad_sym")
    code, captured = sync_cli("commit", "--dry-run")
    assert code == 0
    assert "Add 1 symbol to Conn_XT" in captured.out
    assert "dry run" in captured.out
    assert vcs.read_status(sync_cli.repo).dirty is True


def test_cli_sync_commit_refuses_without_confirmation(sync_cli):
    """
    Same rule as every other mutating command: a non-interactive run must
    pass --yes, so no script can commit the library by accident.
    """
    write(sync_cli.repo, "a.txt")
    code, captured = sync_cli("commit")
    assert code == sync_cli.module.EXIT_ABORTED
    assert vcs.read_status(sync_cli.repo).dirty is True


def test_cli_sync_commit_applies_with_yes(sync_cli):
    write(sync_cli.repo, "symbols/Conn_XT.kicad_symdir/XT60.kicad_sym")
    code, _captured = sync_cli("commit", "--yes")
    assert code == 0
    status = vcs.read_status(sync_cli.repo)
    assert status.dirty is False
    assert status.ahead == 1
    assert status.head.subject == "Add 1 symbol to Conn_XT"


def test_cli_sync_commit_honours_an_explicit_message(sync_cli):
    write(sync_cli.repo, "a.txt")
    code, _captured = sync_cli("commit", "-m", "Park work in progress", "--yes")
    assert code == 0
    assert vcs.read_status(sync_cli.repo).head.subject == "Park work in progress"


def test_cli_sync_commit_on_a_clean_tree_is_a_no_op(sync_cli):
    code, captured = sync_cli("commit", "--yes")
    assert code == 0
    assert "Nothing to commit." in captured.out


def test_cli_sync_fetch(sync_cli, second):
    commit_in(second, "theirs.txt", "theirs")
    git(second, "push")
    code, _captured = sync_cli("fetch")
    assert code == 0
    assert vcs.read_status(sync_cli.repo).behind == 1


def test_cli_sync_pull_audits_what_arrived(sync_cli, second, monkeypatch):
    """
    The audit after a pull is the point of the whole feature: what comes from
    the other machine may be a part whose tables were never regenerated.
    """
    _clean_report(monkeypatch, sync_cli.repo)
    commit_in(second, "theirs.txt", "theirs")
    git(second, "push")
    vcs.fetch(sync_cli.repo)
    code, captured = sync_cli("pull")
    assert code == 0
    assert (sync_cli.repo / "theirs.txt").exists()
    assert "no problems" in captured.out.lower() or "0 error" in captured.out.lower()


def test_cli_sync_pull_fails_when_the_audit_finds_errors(sync_cli, second, monkeypatch):
    _clean_report(monkeypatch, sync_cli.repo, errors=2)
    commit_in(second, "theirs.txt", "theirs")
    git(second, "push")
    vcs.fetch(sync_cli.repo)
    code, captured = sync_cli("pull")
    assert code == sync_cli.module.EXIT_ERROR
    assert "synthetic error" in captured.out
    # The pull itself still happened; the non-zero exit is about what arrived.
    assert (sync_cli.repo / "theirs.txt").exists()


def test_cli_sync_pull_reports_a_refusal(sync_cli, second):
    commit_in(second, "theirs.txt", "theirs")
    git(second, "push")
    commit_in(sync_cli.repo, "ours.txt", "ours")
    vcs.fetch(sync_cli.repo)
    code, captured = sync_cli("pull")
    assert code == sync_cli.module.EXIT_ERROR
    assert "diverged" in captured.out + captured.err


def test_cli_sync_push_refuses_on_audit_errors(sync_cli, monkeypatch):
    _clean_report(monkeypatch, sync_cli.repo, errors=3)
    commit_in(sync_cli.repo, "a.txt", "local")
    code, captured = sync_cli("push", "--yes")
    assert code == sync_cli.module.EXIT_ERROR
    assert "refusing to push with 3 error(s)" in captured.err
    assert vcs.read_status(sync_cli.repo).ahead == 1


def test_cli_sync_push_override_sends_it_anyway(sync_cli, monkeypatch):
    """
    --skip-check exists because this is a one-person library on two
    machines: being unable to park a knowingly-broken state on the remote
    would be worse than the risk of pushing one.
    """
    _clean_report(monkeypatch, sync_cli.repo, errors=3)
    commit_in(sync_cli.repo, "a.txt", "local")
    code, captured = sync_cli("push", "--skip-check", "--yes")
    assert code == 0
    assert "skipping the pre-push audit" in captured.out
    assert vcs.read_status(sync_cli.repo).ahead == 0


def test_cli_sync_push_succeeds_when_the_audit_is_clean(sync_cli, monkeypatch):
    _clean_report(monkeypatch, sync_cli.repo)
    commit_in(sync_cli.repo, "a.txt", "local")
    code, _captured = sync_cli("push", "--yes")
    assert code == 0
    assert vcs.read_status(sync_cli.repo).ahead == 0


def test_cli_sync_push_dry_run_sends_nothing(sync_cli, monkeypatch):
    _clean_report(monkeypatch, sync_cli.repo)
    commit_in(sync_cli.repo, "a.txt", "local")
    code, captured = sync_cli("push", "--dry-run")
    assert code == 0
    assert "dry run" in captured.out
    assert vcs.read_status(sync_cli.repo).ahead == 1


def test_cli_sync_push_reports_a_blocker_rather_than_trying(sync_cli):
    code, captured = sync_cli("push", "--yes")
    assert code == sync_cli.module.EXIT_ERROR
    assert "nothing to push" in captured.err
