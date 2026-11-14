"""Git repository builders run through the sandboxed runner: repos with and
without an origin, bare origins, worktrees, tags and fingerprints, with
the ``make_git_repo*`` and ``ancestor_free_dir`` fixtures. A registered
plugin. Moved from ``tests/conftest.py`` (#2668).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import tests._session_home
from cw.config import load_state, save_state
from cw.models import (
    ClientConfig,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from cw.native_daemon import FakeNativeDaemonClient
from tests import _git_sandbox

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def ancestor_free_dir(tmp_path: Path) -> Path:
    """A path directly under the filesystem root, never created.

    For tests that walk the filesystem upward in Python (``find_cw_context``,
    ``repo_root``), which no git ceiling can confine: no ancestor of this path
    can be a checkout, so the walk finds nothing even when ``TMPDIR`` is inside
    one. Relies on ``/.claude/cw-context.json`` and ``/pyproject.toml`` being
    absent on the host.
    """
    return Path(tmp_path.anchor) / "cw-test-ancestor-free"


def _clean_git_env() -> dict[str, str]:
    """The env a test hands a child that runs git it cannot route through the runner.

    The sandbox env (``tests/_git_sandbox.py``: every ``GIT_*`` stripped, the
    basetemp ceiling, ``GIT_CONFIG_NOSYSTEM``, a root-local ``HOME``) while a
    sandbox root is bound and enforced -- so a bash script under test gets the
    same confinement as the runner -- else ``os.environ`` with ``GIT_*``
    stripped. Either way a nested git never inherits a wrapping git call's env
    (e.g. a hook's ``GIT_DIR``).
    """
    root = _git_sandbox.bound_root()
    if root is not None and _git_sandbox.containment_enforced():
        return _git_sandbox.sandbox_env(root)
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git_in(repo: Path, *args: str) -> str:
    """Run git in *repo* through the sandboxed runner, returning stripped stdout.

    Canonical runner for tests that drive a real ``make_git_repo`` repo through
    raw git commands; a thin name for ``tests._git_sandbox.git`` (#3078), which
    strips every ``GIT_*`` (pytest may itself be running inside a git hook,
    whose ``GIT_DIR``/``GIT_INDEX_FILE`` would otherwise redirect the nested
    invocation away from *repo*) and refuses paths outside the basetemp.
    Consolidates four byte-identical private copies (``test_branch_ahead.py``,
    ``test_dispatch_branch_freshness.py``, ``test_worktree.py``, and
    ``test_dispatch.py``'s ``_git_in_repo``) and the further private copies
    consolidated by #2195.
    """
    return _git_sandbox.git(repo, *args)


def list_tags(repo: Path) -> list[str]:
    """Return ``git tag --list`` for *repo*, one tag per entry, blank lines dropped.

    Hoisted from ``test_release_sh.py``'s private ``_tags`` (#2304) so the
    CHANGELOG-freeze gate's tests share one tag-listing helper.
    """
    return [line for line in git_in(repo, "tag", "--list").splitlines() if line]


def write_pyproject_override(repo: Path, toml_body: str) -> Path:
    """Write *toml_body* as ``<repo>/pyproject.toml`` and return its path.

    The fixture writer for ``[tool.cw.<name>]`` per-repo override tables,
    hoisted from ``test_check_plan_scope_conformance.py`` (#2304).
    """
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(toml_body, encoding="utf-8")
    return pyproject


@pytest.fixture
def make_git_repo(tmp_path: Path) -> Callable[..., Path]:
    """Factory fixture to create git repos in tmp_path.

    Initialises with a single empty commit on ``main`` so callers that
    invoke ``git worktree add`` (notably dispatch / pr_responder tests)
    have a real commit to branch from. Sets per-repo user.name/email so
    the commit succeeds without a global git config (CI runners often
    lack one).

    The keyword-only ``base`` overrides the parent directory (default
    ``tmp_path``). The live codex contract suite (#1238) passes a home-tree
    base because snap-confined codex cannot reach ``/tmp``; every pre-existing
    positional caller keeps its exact ``tmp_path``-relative behavior.
    """

    def _make(name: str, *, base: Path | None = None) -> Path:
        repo = (base if base is not None else tmp_path) / name
        repo.mkdir(parents=True, exist_ok=True)

        def _git(*args: str) -> None:
            _git_sandbox.run(*args, cwd=repo)

        _git("init", "-b", "main")
        _git("config", "user.email", tests._session_home.TEST_GIT_USER_EMAIL)
        _git("config", "user.name", tests._session_home.TEST_GIT_USER_NAME)
        _git("commit", "--allow-empty", "-m", "initial")
        return repo

    return _make


def init_repo_with_remote(path: Path, remote_url: str | None) -> Path:
    """Build a minimal git repo at *path*, optionally with an ``origin`` remote.

    Plain runner calls (not ``make_git_repo``): no existing fixture sets a
    remote, and extending the widely-shared factory to add one would be a
    broader-blast-radius change than the callers need (#1198). Shared by
    ``test_pr_hydrate.py`` and ``test_preflight.py`` (#2158) so the same
    ~10-line builder isn't maintained as two independent copies.
    """
    path.mkdir(parents=True, exist_ok=True)
    _git_sandbox.run("init", cwd=str(path), text=False)
    if remote_url is not None:
        _git_sandbox.run(
            "remote", "add", "origin", remote_url, cwd=str(path), text=False
        )
    return path


def commit_tracked_file(worktree: Path, relpath: str, content: str = "x = 1\n") -> None:
    """Write *relpath* under *worktree* and commit it as a real tracked file.

    Shared by tests that need a ``make_git_repo`` worktree to carry tracked
    files beyond its base empty commit — cw #1915's ``build_aiderignore``
    exercises ``git ls-files`` against a real tracked-file set, and its tests
    (plus the corresponding executor spawn test) all need this same
    mkdir/write/add/commit sequence. Runs through the sandboxed runner, so the
    nested git invocation doesn't inherit a wrapping git call's env.
    """
    path = worktree / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git_sandbox.run("add", relpath, cwd=worktree)
    _git_sandbox.run("commit", "-m", f"add {relpath}", cwd=worktree)


def commit_small_py_files(
    repo: Path, count: int, *, branch: str = "feature", prefix: str = "mod"
) -> list[str]:
    """Check out *branch* in *repo* and commit *count* one-line ``.py`` files.

    The "N small python files on a branch" builder the #2086 ``large_diff``
    tests share (``test_codex_review_diff.py`` and
    ``test_codex_review_context.py``): a count past the file cap makes the
    branch ``large_diff`` while its line total stays tiny. Returns the
    committed relative paths in creation order.
    """
    _git_sandbox.run("checkout", "-b", branch, cwd=repo)
    paths = [f"{prefix}_{index}.py" for index in range(count)]
    for relpath in paths:
        (repo / relpath).write_text(f"value_{relpath[:-3]} = 1\n", encoding="utf-8")
    _git_sandbox.run("add", *paths, cwd=repo)
    _git_sandbox.run("commit", "-m", f"add {count} small modules", cwd=repo)
    return paths


#: The non-UTF-8 byte string the #3419 tests name a file with.
NON_UTF8_NAME_BYTES = b"bad-\xff.txt"

#: Shell twin of :func:`replace_dir_with_file` for a gate-level autofix command.
DIR_TO_FILE_SH = "rm -r pkg && printf x > pkg"


def write_non_utf8_named_file(repo: Path, content: str = "x\n") -> str:
    """Create a file whose name is not valid UTF-8 and return its ``fsdecode`` name.

    Skips off Linux: macOS (APFS) rejects non-UTF-8 filenames. The returned
    name carries a lone surrogate; ``os.fsencode`` gives the original bytes back.
    """
    if sys.platform != "linux":
        pytest.skip("non-UTF-8 filenames need a Linux filesystem")
    name = os.fsdecode(NON_UTF8_NAME_BYTES)
    (repo / name).write_text(content, encoding="utf-8")
    return name


def replace_dir_with_file(repo: Path, name: str = "pkg") -> None:
    """Turn the directory *name* into a regular file of the same name.

    The type change ``git restore`` cannot undo path by path (#3423).
    """
    shutil.rmtree(repo / name)
    (repo / name).write_text("x", encoding="utf-8")


def hostile_git_env(
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
) -> tuple[Path, Path]:
    """Build a target repo plus a decoy whose ``GIT_DIR`` the env points at.

    The decoy carries a commit of its own so its HEAD differs from the
    target's (``make_git_repo``'s base commit is byte-identical everywhere).
    """
    repo = make_git_repo(f"{prefix}-target")
    decoy = make_git_repo(f"{prefix}-decoy")
    commit_tracked_file(decoy, "decoy.py", "decoy = True\n")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    return repo, decoy


def push_commit_to_origin(
    origin: Path,
    branch: str,
    work_dir: Path,
    filename: str,
    content: str = "out-of-band\n",
) -> str:
    """Push one new commit to *branch* on the bare *origin* from a side clone.

    Simulates an out-of-band push (another machine, a sibling session, a fix
    agent's isolation worktree) that advances ``origin/<branch>`` without
    touching the workspace under test. *branch* must already exist on *origin*.

    The clone at *work_dir* is created only when *work_dir* does not yet exist;
    a second call in the same test reuses it, re-syncing to the current remote
    tip first so the new commit lands on top of whatever was pushed since.
    Writes *content* to *filename* so a test can make upstream set a value that
    differs from a local commit. Returns the pushed commit's SHA.
    """
    if not work_dir.exists():
        _git_sandbox.run("clone", str(origin), str(work_dir))
    git_in(work_dir, "fetch", "origin")
    git_in(work_dir, "checkout", "-B", branch, f"origin/{branch}")
    (work_dir / filename).write_text(content, encoding="utf-8")
    git_in(work_dir, "add", filename)
    git_in(
        work_dir,
        "-c",
        f"user.email={tests._session_home.TEST_GIT_USER_EMAIL}",
        "-c",
        f"user.name={tests._session_home.TEST_GIT_USER_NAME}",
        "commit",
        "-m",
        f"out-of-band {filename}",
    )
    git_in(work_dir, "push", "origin", branch)
    return git_in(work_dir, "rev-parse", "HEAD")


def init_bare_origin(repo: Path) -> Path:
    """Create and attach a bare ``origin`` beside *repo*.

    Scenario-specific callers decide which commits and branches to push after
    this shared initialization and remote wiring.
    """
    origin = repo.parent / f"{repo.name}-origin.git"
    _git_sandbox.run("init", "--bare", "-b", "main", str(origin), text=False)
    git_in(repo, "remote", "add", "origin", str(origin))
    return origin


def add_bare_origin(repo: Path) -> Path:
    """Attach a bare ``origin`` beside *repo* and push every local branch to it.

    Gives a ``make_git_repo`` repo the remote that the codex fix loop's
    per-cycle push (#2354) needs. Every branch is pushed, not only the checked
    out one, so ``origin/main`` exists for any diff-base resolution too.
    Returns the bare origin's path.
    """
    origin = init_bare_origin(repo)
    git_in(repo, "push", "origin", "--all")
    git_in(repo, "fetch", "origin")
    return origin


@pytest.fixture
def make_git_repo_with_origin(
    make_git_repo: Callable[..., Path],
) -> Callable[..., tuple[Path, Path]]:
    """Factory: a repo on a feature *branch* already pushed to a bare origin.

    Returns ``(worktree, origin)``. The feature branch carries one tracked
    commit beyond ``main`` so it is a real, non-empty branch (#2354).
    """

    def _make(name: str, *, branch: str = "feature") -> tuple[Path, Path]:
        repo = make_git_repo(name)
        git_in(repo, "checkout", "-b", branch)
        commit_tracked_file(repo, "feature.py")
        return repo, add_bare_origin(repo)

    return _make


@pytest.fixture
def make_worktree_with_change(
    make_git_repo: Callable[..., Path],
) -> Callable[..., Path]:
    """Factory: a repo on ``feature`` with *content* committed to *filename*.

    The branch is pushed to a bare origin: the codex fix loop's per-cycle push
    and the review-exit guard compare HEAD with ``origin/<branch>`` (#2354).
    Hoisted (#2388) from two private ``_worktree_with_change`` copies in
    ``test_codex_executor.py`` and ``test_codex_driver.py``.
    """

    def _make(name: str, *, filename: str, content: str) -> Path:
        repo = make_git_repo(name)
        git_in(repo, "checkout", "-b", "feature")
        (repo / filename).write_text(content, encoding="utf-8")
        git_in(repo, "add", filename)
        git_in(repo, "commit", "-m", f"add {filename}")
        add_bare_origin(repo)
        return repo

    return _make


def tree_fingerprint(worktree: Path) -> tuple[str, str, str]:
    """``(HEAD sha, porcelain status, digest of every working-tree file)``.

    Byte-level: two equal fingerprints mean nothing moved HEAD, the index or a
    single file's content in the worktree. Hoisted (#2213 round 5) from
    ``test_reconcile_review_recipes.py`` once the dispatch claim tests needed
    the same "nothing touched the occupied worktree" proof.
    """
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(worktree.rglob("*")):
        rel = path.relative_to(worktree)
        if rel.parts[0] == ".git" or not path.is_file():
            continue
        digest.update(str(rel).encode())
        digest.update(path.read_bytes())
    return (
        git_in(worktree, "rev-parse", "HEAD"),
        git_in(worktree, "status", "--porcelain=v1", "--untracked-files=all"),
        digest.hexdigest(),
    )


def occupy_worktree(
    client: ClientConfig,
    worktree: Path,
    source: str,
    *,
    daemon: FakeNativeDaemonClient | None = None,
) -> None:
    """Make a live occupant appear for *worktree* through the named source.

    ``state`` (a non-terminal cw session homed there) and ``roster`` (a live
    daemon worker with that ``cwd``) are positive matches; ``unreadable-roster``
    is the fail-closed case (occupancy cannot be ruled out). Shared by the
    fix-agent and dispatch-claim refusal tests (#2213 round 5).

    #2480: a daemon-roster worker now counts as occupying only when its
    ``surface_ref`` names a non-terminal cw session, so the ``roster`` source
    also seeds a vouching session (``worktree_path=None`` -- it must vouch for
    the worker's surface_ref only, not independently satisfy the ``state``
    side) carrying that worker's short id as ``surface_ref``.

    *daemon* (#2213 round 7): pass the SAME :class:`FakeNativeDaemonClient`
    instance the caller is about to inject into ``create_worktree`` /
    ``dispatch_tick`` / ``_spawn_claimed_task``. Occupancy checks now consult
    the caller's own resolved daemon rather than defaulting to the real one, so
    a ``roster``/``unreadable-roster`` occupant written to the real (tmp-
    isolated) roster file would go unseen by an injected fake -- writing to the
    fake directly is what makes those sources observable again. Omit it (the
    ``state`` source is unaffected either way) only when the caller relies on
    ``create_worktree``'s own default (no injected daemon).
    """
    from cw import native_daemon
    from cw.config import load_state

    if source == "state":
        state = load_state()
        state.sessions.append(
            Session(
                name=f"{client.name}/impl/occupant",
                client=client.name,
                purpose=SessionPurpose.IMPL,
                origin=SessionOrigin.USER,
                workspace_path=client.workspace_path,
                worktree_path=worktree,
                status=SessionStatus.ACTIVE,
            )
        )
        save_state(state)
        return
    if daemon is not None:
        if source == "roster":
            short_id = daemon.seed_live_worker(worktree)
            _vouch_for_roster_worker(client, short_id)
        else:
            daemon.roster_unreadable = True
        return
    roster = native_daemon._ROSTER_PATH
    roster.parent.mkdir(parents=True, exist_ok=True)
    if source == "roster":
        payload = {"workers": {"aaaa1111": {"pid": 1, "cwd": str(worktree)}}}
        roster.write_text(json.dumps(payload), encoding="utf-8")
        _vouch_for_roster_worker(client, "aaaa1111")
    else:
        roster.write_text("{not json", encoding="utf-8")


def _vouch_for_roster_worker(client: ClientConfig, short_id: str) -> None:
    """Seed a non-terminal session vouching for *short_id* (#2480).

    Companion to :func:`occupy_worktree`'s ``roster`` source: appends rather
    than overwrites, so it composes with a ``state``-source occupant already
    present in the same test's cw state.

    ``status=BACKGROUNDED`` (not ``ACTIVE``) deliberately: BACKGROUNDED is
    still non-terminal for occupancy purposes (``_NON_TERMINAL_SESSION_
    STATUSES`` in ``cw.worktree._liveness`` includes it), but unlike ACTIVE/
    IDLE it is NOT counted by dispatch's ``running_count`` (session-based
    client slot budget, ``cw.dispatch.tick``) -- an ACTIVE vouch session here
    would silently consume the client's entire slot budget in a
    ``dispatch_tick``-driving test, starving ``available_client_slots`` to 0
    and skipping the pre-claim occupancy screen entirely before it ever
    reaches ``live_home_reason`` (discovered live while adding this helper).
    """
    state = load_state()
    state.sessions.append(
        Session(
            name=f"{client.name}/impl/occupant-vouch",
            client=client.name,
            purpose=SessionPurpose.IMPL,
            origin=SessionOrigin.DAEMON,
            workspace_path=client.workspace_path,
            worktree_path=None,
            status=SessionStatus.BACKGROUNDED,
            surface_ref=short_id,
        )
    )
    save_state(state)
