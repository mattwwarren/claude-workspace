"""Tests for the repair_mechanical_conflict review recipe (#2917).

Hermetic: ``ScriptedGit`` stands in for ``run_git``, ``FakeGhHead`` for the
gh head read and disarm, ``FakeArmScript`` for the arm subprocess, and the
local :class:`ScriptedCommands` for ``run_declared_command`` (the operator's
repair and verify commands). Real-git coverage lives in
``tests/test_mechanical_repair_real_git.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from freezegun import freeze_time

from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import ClientConfig, DevQueueStore, LaneConfig, QueueItemStatus
from cw.reconcile.deferred import DeferredReconcileJobs, run_post_lock_jobs
from cw.reconcile.review_recipes import (
    RECIPE_ATTENTION_STATES,
    RECIPE_FIRED_AT_GETTERS,
    RECIPE_REPAIR_CHANGELOG_CONFLICT,
    RECIPE_REPAIR_MECHANICAL_CONFLICT,
    DeferredReviewDispatch,
    run_review_recipes,
)
from cw.reconcile.review_recipes import mechanical_repair_git as mgit
from cw.reconcile.review_recipes._shared import RepoSlugs
from cw.reconcile.review_recipes.changelog_repair import (
    _LATCH as _CHANGELOG_LATCH,
)
from cw.reconcile.review_recipes.core import capture_review_repo_slugs
from cw.reconcile.review_recipes.mechanical_repair import (
    _LATCH,
    MECHANICAL_SPEC,
    _act_repair_mechanical_conflict,
    _detect_repair_mechanical_conflict,
)
from cw.reconcile.review_recipes.mechanical_repair_git import (
    CommandOutcome,
    path_in_scope,
    resolve_argv,
)
from cw.reconcile.review_recipes.repair_engine import _MAX_ATTEMPTS, dispatch_repair
from cw.repair_disposition import RepairDisposition as D
from tests._changelog_repair_helpers import (
    BRANCH,
    DEFAULT_REF,
    EXPECTED_SHA,
    MB_SHA,
    NEW_SHA,
    PR_NUMBER,
    PR_URL,
    TICKET,
    FakeArmScript,
    FakeGhHead,
    GitResult,
    LogEntry,
    Response,
    ScriptedGit,
    act_clients_for,
    act_once,
    attention,
    config,
    enabling_clients_for,
    failed,
    make_client,
    make_repair_job,
    mkdir_scratch,
    repair_task,
    rm_scratch,
    scratch_path,
    taken,
)
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._review_convergence_helpers import confirmed_ledger, write_ledger
from tests.conftest import init_repo_with_remote
from tests.test_pr_hydrate import _pr_state

pytestmark = pytest.mark.usefixtures("repair_events")

CONFLICT = "migrations/0002.py"
OUT_OF_GLOB = "src/app.py"
REPAIR_ARGV = ["/opt/cw-test/rechain.sh", "--fix"]
VERIFY_ARGV = ["/opt/cw-test/single_head.sh"]
ENTRY: dict[str, Any] = {
    "paths": ["migrations/**"],
    "repair_command": REPAIR_ARGV,
    "verify_command": VERIFY_ARGV,
    "repair_timeout_seconds": 300,
    "verify_timeout_seconds": 120,
}
_KEY = ("acme", TICKET, PR_URL)
_FROZEN = "2026-01-01T00:00:00Z"
_TAKEN_EVIDENCE_KEYS = {
    "merge_state_status",
    "mergeable",
    "conflicted_paths",
    "expected_sha",
    "new_sha",
    "auto_merge_was_armed",
}


@pytest.fixture(autouse=True)
def _frozen_clock() -> Iterator[None]:
    """Every hermetic test here records events at one frozen instant."""
    with freeze_time(_FROZEN):
        yield


# --- ScriptedCommands -------------------------------------------------------


class CommandCall(NamedTuple):
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    timeout: float


CommandResponse = (
    CommandOutcome | BaseException | Callable[[CommandCall], CommandOutcome]
)


class ScriptedCommands:
    """A ``run_declared_command`` fake: per-``argv[0]`` scripted outcomes."""

    def __init__(self, log: list[LogEntry]) -> None:
        self.log = log
        self.calls: list[CommandCall] = []
        self.responses: dict[str, CommandResponse] = {}

    def set(self, argv0: str, response: CommandResponse) -> None:
        self.responses[argv0] = response

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
    ) -> CommandOutcome:
        call = CommandCall(tuple(argv), cwd, dict(env), timeout)
        self.calls.append(call)
        self.log.append(("cmd", argv[0]))
        response = self.responses.get(argv[0], CommandOutcome(0, "ok", False))
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, CommandOutcome):
            return response
        return response(call)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> ScriptedCommands:
        monkeypatch.setattr(mgit, "run_declared_command", self)
        return self


def _z(*paths: str) -> str:
    return "".join(f"{path}\0" for path in paths)


def merge_happy_rules(
    *,
    conflicts: tuple[str, ...] = (CONFLICT,),
    unmerged: tuple[str, ...] | None = None,
    status: str | None = None,
    branch_names: tuple[str, ...] = (OUT_OF_GLOB, CONFLICT),
) -> list[tuple[tuple[str, ...], Response]]:
    """Responses for a merge-shaped repair that succeeds through the push.

    The CHANGELOG hidden-conflict probe (``diff --name-only <a> <b> --
    CHANGELOG.md``) would report CHANGELOG.md changed on both sides, so a
    recipe that issued it would add CHANGELOG.md to its conflict set.
    """
    in_merge = conflicts if unmerged is None else unmerged
    porcelain = (
        status
        if status is not None
        else "".join(f"UU {path}\0" for path in in_merge)
        + "M  migrations/0002_main.py\0"
    )
    return [
        (("fetch", "origin"), GitResult()),
        (("rev-parse", "--verify", "--quiet"), GitResult(stdout=f"{EXPECTED_SHA}\n")),
        (("merge-base", DEFAULT_REF), GitResult(stdout=f"{MB_SHA}\n")),
        (
            ("merge-tree",),
            GitResult(
                1 if conflicts else 0, "tree0\0" + "".join(f"{c}\0" for c in conflicts)
            ),
        ),
        (("merge", "--no-commit"), GitResult(1, "", "CONFLICT (content)")),
        (
            ("diff", "--name-only", "-z", "--diff-filter=U"),
            GitResult(stdout=_z(*in_merge)),
        ),
        (
            ("diff", "--name-only", "-z", "--no-renames"),
            GitResult(stdout=_z(*branch_names)),
        ),
        (("diff", "--name-only"), GitResult(stdout="CHANGELOG.md\n")),
        (("status", "--porcelain=v1"), GitResult(stdout=porcelain)),
        (("--literal-pathspecs", "diff", "-U0"), GitResult(stdout="+x = 1\n")),
        (("worktree", "add"), mkdir_scratch),
        (("worktree", "remove"), rm_scratch),
        (("rev-parse", "HEAD"), GitResult(stdout=f"{NEW_SHA}\n")),
        (("merge-base", "--is-ancestor"), GitResult()),
        (("ls-remote",), GitResult(stdout=f"{NEW_SHA}\trefs/heads/{BRANCH}\n")),
    ]


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def git(monkeypatch: pytest.MonkeyPatch, log: list[LogEntry]) -> ScriptedGit:
    return ScriptedGit(log, merge_happy_rules()).install(monkeypatch)


@pytest.fixture
def cmds(monkeypatch: pytest.MonkeyPatch, log: list[LogEntry]) -> ScriptedCommands:
    return ScriptedCommands(log).install(monkeypatch)


@pytest.fixture
def client(repair_workspace: Path) -> ClientConfig:
    return make_client(repair_workspace, mechanical_conflict_repair=ENTRY)


def _dispatch(client: ClientConfig, **kwargs: Any) -> str | None:
    return dispatch_repair(make_repair_job(client, spec=MECHANICAL_SPEC, **kwargs))


def _verbs(git: ScriptedGit) -> list[str]:
    return [argv[0] for argv in git.stripped()]


def _has(git: ScriptedGit, *prefix: str) -> bool:
    return any(argv[: len(prefix)] == prefix for argv in git.stripped())


def _only_failure(disposition: D) -> dict[str, Any]:
    assert taken() == []
    (failure,) = failed()
    assert failure["disposition"] == disposition.value
    assert failure["recipe"] == RECIPE_REPAIR_MECHANICAL_CONFLICT
    assert failure["conflict_kind"] == "mechanical_paths_candidate"
    return failure


def _assert_untouched(git: ScriptedGit, arm: FakeArmScript | None = None) -> None:
    assert not _has(git, "push")
    if arm is not None:
        assert arm.calls() == []


# --- detect -----------------------------------------------------------------


def _entry_clients(*, lane_value: bool | None = True) -> dict[str, ClientConfig]:
    recipes = (
        None if lane_value is None else {RECIPE_REPAIR_MECHANICAL_CONFLICT: lane_value}
    )
    lane = LaneConfig(name="default", review_recipes=recipes)
    return {
        "acme": ClientConfig(
            name="acme",
            workspace_path=Path("/tmp/ws"),
            default_branch="main",
            lanes=[lane],
            mechanical_conflict_repair=ENTRY,
        )
    }


def _detect(task: Any, clients: Any = None, cfg: Any = None) -> list[Any]:
    return _detect_repair_mechanical_conflict(
        [task],
        clients=clients if clients is not None else _entry_clients(),
        config=cfg if cfg is not None else config(),
    )


def test_no_entry_is_never_a_candidate() -> None:
    clients = enabling_clients_for(RECIPE_REPAIR_MECHANICAL_CONFLICT)

    assert _detect(repair_task(), clients=clients) == []


def test_entry_makes_conflicting_dirty_completed_row_a_candidate() -> None:
    (cand,) = _detect(repair_task(), clients=_entry_clients(lane_value=None))

    assert cand.recipe == RECIPE_REPAIR_MECHANICAL_CONFLICT
    assert cand.attention_state == "merge_blocked"


@pytest.mark.parametrize(
    "task",
    [
        pytest.param(repair_task(status=QueueItemStatus.RUNNING), id="running"),
        pytest.param(
            repair_task(
                pr_state=_pr_state(
                    mergeable="CONFLICTING",
                    merge_state_status="BLOCKED",
                    attention_state="merge_blocked",
                )
            ),
            id="not-dirty",
        ),
    ],
)
def test_not_completed_or_not_dirty_is_not_a_candidate(task: Any) -> None:
    assert _detect(task) == []


def test_master_switch_off_no_candidate() -> None:
    assert _detect(repair_task(), cfg=config(review_recipes_enabled=False)) == []


def test_lane_false_vetoes_entry() -> None:
    assert _detect(repair_task(), clients=_entry_clients(lane_value=False)) == []


# --- act --------------------------------------------------------------------


@pytest.fixture
def act_clients(repair_workspace: Path) -> dict[str, ClientConfig]:
    return act_clients_for(
        repair_workspace,
        RECIPE_REPAIR_MECHANICAL_CONFLICT,
        mechanical_conflict_repair=ENTRY,
    )


def _act(clients: dict[str, ClientConfig], **kwargs: Any) -> Any:
    detect, act = _detect_repair_mechanical_conflict, _act_repair_mechanical_conflict
    return act_once(detect, act, clients, **kwargs)


def test_act_returns_one_job_per_tick_and_is_subprocess_free(
    act_clients: dict[str, ClientConfig], git: ScriptedGit, cmds: ScriptedCommands
) -> None:
    save_dev_queue(DevQueueStore(tasks=[repair_task(), repair_task(ticket_id="GEN-2")]))

    (job,) = _act(act_clients)

    assert job.spec is MECHANICAL_SPEC
    assert job.payload_base["conflict_kind"] == "mechanical_paths_candidate"
    assert job.payload_base["evidence_snapshot"]["conflicted_paths"] is None
    assert git.calls == []
    assert cmds.calls == []
    assert read_events() == []


def test_act_latch_blocks_after_max_attempts(
    act_clients: dict[str, ClientConfig],
) -> None:
    save_dev_queue(DevQueueStore(tasks=[repair_task()]))
    for _ in range(_MAX_ATTEMPTS):
        assert len(_act(act_clients)) == 1

    assert _act(act_clients) == []
    assert _LATCH.blocked(_KEY)


def test_act_skips_excluded_rows_without_bumping_latch(
    act_clients: dict[str, ClientConfig],
) -> None:
    save_dev_queue(DevQueueStore(tasks=[repair_task()]))

    assert _act(act_clients, exclude=frozenset({("acme", TICKET)})) == []

    assert _LATCH.attempts(_KEY) == 0
    assert read_events() == []


def test_cross_repo_mismatch_emits_failed(
    act_clients: dict[str, ClientConfig], repair_workspace: Path
) -> None:
    init_repo_with_remote(repair_workspace, "https://github.com/other/thing.git")
    save_dev_queue(DevQueueStore(tasks=[repair_task()]))

    assert _act(act_clients) == []

    (failure,) = failed()
    assert "repo mismatch" in failure["error"]
    assert failure["recipe"] == RECIPE_REPAIR_MECHANICAL_CONFLICT


def test_slug_miss_defers_without_bumping_latch(
    act_clients: dict[str, ClientConfig],
) -> None:
    save_dev_queue(DevQueueStore(tasks=[repair_task()]))

    assert _act(act_clients, slugs=RepoSlugs()) == []

    assert _LATCH.attempts(_KEY) == 0


# --- conflict handling ------------------------------------------------------


@pytest.mark.usefixtures("head")
def test_out_of_glob_conflict_parks_out_of_scope_and_touches_nothing(
    git: ScriptedGit, cmds: ScriptedCommands, client: ClientConfig, arm: FakeArmScript
) -> None:
    git.rules = merge_happy_rules(conflicts=(CONFLICT, OUT_OF_GLOB))

    assert _dispatch(client) is None

    failure = _only_failure(D.OUT_OF_SCOPE_CONFLICT)
    assert OUT_OF_GLOB in failure["error"]
    assert not _has(git, "worktree", "add")
    assert not _has(git, "merge", "--no-commit")
    assert cmds.calls == []
    _assert_untouched(git, arm)
    assert _LATCH.blocked(_KEY)


@pytest.mark.usefixtures("head", "arm")
def test_in_glob_only_conflict_is_repaired(
    git: ScriptedGit, cmds: ScriptedCommands, client: ClientConfig
) -> None:
    assert _dispatch(client) == TICKET

    assert _has(git, "push")
    assert [call.argv[0] for call in cmds.calls] == [REPAIR_ARGV[0], VERIFY_ARGV[0]]
    assert failed() == []
    assert _LATCH.blocked(_KEY)


@pytest.mark.usefixtures("head", "arm", "cmds")
def test_changelog_touched_on_both_sides_does_not_park_in_glob_conflict(
    git: ScriptedGit, client: ClientConfig
) -> None:
    assert _dispatch(client) == TICKET

    assert _has(git, "push")
    probes = [
        argv
        for argv in git.stripped()
        if argv[:2] == ("diff", "--name-only") and argv[-1:] == ("CHANGELOG.md",)
    ]
    assert probes == []
    (done,) = taken()
    assert done["evidence_snapshot"]["conflicted_paths"] == [CONFLICT]


@pytest.mark.usefixtures("head")
def test_mixed_changelog_and_migration_conflict_parks(
    git: ScriptedGit, cmds: ScriptedCommands, client: ClientConfig, arm: FakeArmScript
) -> None:
    git.rules = merge_happy_rules(conflicts=("CHANGELOG.md", "migrations/x"))

    assert _dispatch(client) is None

    failure = _only_failure(D.OUT_OF_SCOPE_CONFLICT)
    assert "CHANGELOG.md" in failure["error"]
    assert not _has(git, "worktree", "add")
    assert cmds.calls == []
    _assert_untouched(git, arm)


@pytest.mark.usefixtures("head", "git")
def test_entry_missing_at_dispatch_parks_merge_failed(
    cmds: ScriptedCommands, repair_workspace: Path
) -> None:
    bare = make_client(repair_workspace)

    assert _dispatch(bare) is None

    failure = _only_failure(D.MERGE_FAILED)
    assert "mechanical_conflict_repair" in failure["error"]
    assert cmds.calls == []


@pytest.mark.usefixtures("head")
def test_empty_conflict_set_parks_conflict_unexplained(
    git: ScriptedGit, cmds: ScriptedCommands, client: ClientConfig
) -> None:
    git.rules = merge_happy_rules(conflicts=())

    assert _dispatch(client) is None

    failure = _only_failure(D.CONFLICT_UNEXPLAINED)
    assert failure["evidence_snapshot"]["conflicted_paths"] == []
    assert cmds.calls == []


@pytest.mark.usefixtures("head", "arm")
def test_clean_merge_after_conflict_set_parks_conflict_unexplained(
    git: ScriptedGit, cmds: ScriptedCommands, client: ClientConfig
) -> None:
    git.set(("merge", "--no-commit"), GitResult())

    assert _dispatch(client) is None

    _only_failure(D.CONFLICT_UNEXPLAINED)
    assert cmds.calls == []
    _assert_untouched(git)


@pytest.mark.usefixtures("head", "arm")
@pytest.mark.parametrize("stray", ["CHANGELOG.md", OUT_OF_GLOB])
def test_merge_unmerged_paths_outside_globs_parks(
    git: ScriptedGit, cmds: ScriptedCommands, client: ClientConfig, stray: str
) -> None:
    git.rules = merge_happy_rules(unmerged=(CONFLICT, stray))

    assert _dispatch(client) is None

    failure = _only_failure(D.OUT_OF_SCOPE_CONFLICT)
    assert stray in failure["error"]
    assert cmds.calls == []
    assert not _has(git, "commit")
    _assert_untouched(git)
    assert not scratch_path(client, MECHANICAL_SPEC).exists()


@pytest.mark.usefixtures("head", "arm")
@pytest.mark.parametrize(
    ("returncode", "unmerged"),
    [pytest.param(1, (), id="exit1-nothing-unmerged"), pytest.param(2, (), id="exit2")],
)
def test_merge_that_fails_outright_parks_merge_failed(
    git: ScriptedGit,
    cmds: ScriptedCommands,
    client: ClientConfig,
    returncode: int,
    unmerged: tuple[str, ...],
) -> None:
    git.set(("merge", "--no-commit"), GitResult(returncode, "", "fatal: refusing"))
    git.set(
        ("diff", "--name-only", "-z", "--diff-filter=U"),
        GitResult(stdout=_z(*unmerged)),
    )

    assert _dispatch(client) is None

    failure = _only_failure(D.MERGE_FAILED)
    assert "git merge" in failure["error"]
    assert cmds.calls == []


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("migrations/**", "migrations/versions/0001_a.py", True),
        ("migrations/**", "migrations/0001.py", True),
        ("migrations/*.py", "migrations/0001.py", True),
        ("migrations/*.py", "migrations/v/0001.py", False),
        ("migrations/**", "app/migrations/0001.py", False),
        ("migrations/**", "migrations_old/0001.py", False),
        ("max_migration.txt", "max_migration.txt", True),
        ("max_migration.txt", "sub/max_migration.txt", False),
    ],
)
def test_glob_semantics(pattern: str, path: str, expected: bool) -> None:
    assert path_in_scope([pattern])(path) is expected


# --- repair command ---------------------------------------------------------


@pytest.mark.usefixtures("head", "arm", "git")
def test_repair_command_invoked_as_argv_list_in_scratch_tree(
    monkeypatch: pytest.MonkeyPatch, cmds: ScriptedCommands, client: ClientConfig
) -> None:
    monkeypatch.setenv("GIT_DIR", "/nowhere")

    assert _dispatch(client) == TICKET

    repair, verify = cmds.calls
    scratch = scratch_path(client, MECHANICAL_SPEC)
    assert list(repair.argv) == REPAIR_ARGV
    assert repair.cwd == scratch
    assert repair.timeout == ENTRY["repair_timeout_seconds"]
    assert repair.env["CW_REPAIR_DEFAULT_REF"] == DEFAULT_REF
    assert repair.env["CW_REPAIR_MERGE_BASE"] == MB_SHA
    assert repair.env["CW_REPAIR_BRANCH"] == BRANCH
    assert repair.env["CW_REPAIR_CONFLICTED_PATHS"] == CONFLICT
    assert not any(key.startswith("GIT_") for key in repair.env)
    assert list(verify.argv) == VERIFY_ARGV
    assert verify.cwd == scratch
    assert verify.timeout == ENTRY["verify_timeout_seconds"]
    assert verify.env == repair.env


_ENV_PATH_NAMES = [
    (2, "a"),
    (3, "b"),
    (9, "c"),
    (11, "d"),
    (15, "e"),
    (21, "f"),
    (30, "g"),
]


@pytest.mark.usefixtures("head", "arm")
def test_conflicted_paths_env_is_newline_joined_and_sorted(
    git: ScriptedGit, cmds: ScriptedCommands, client: ClientConfig
) -> None:
    # Out of order, and enough paths that a frozenset's hash order cannot
    # coincide with the sorted order for every hash seed.
    expected = [f"migrations/{n:04d}_{name}.py" for n, name in _ENV_PATH_NAMES]
    git.rules = merge_happy_rules(unmerged=tuple(reversed(expected)))

    assert _dispatch(client) == TICKET

    repair, _verify = cmds.calls
    assert repair.env["CW_REPAIR_CONFLICTED_PATHS"] == "\n".join(expected)


@pytest.mark.usefixtures("head", "arm", "git")
def test_relative_command_resolves_against_client_workspace_not_scratch(
    cmds: ScriptedCommands, repair_workspace: Path
) -> None:
    entry = {**ENTRY, "repair_command": ["scripts/rechain.sh", "scripts/arg.txt"]}
    client = make_client(repair_workspace, mechanical_conflict_repair=entry)

    assert _dispatch(client) == TICKET

    repair = cmds.calls[0]
    assert repair.argv == (
        str(repair_workspace / "scripts/rechain.sh"),
        "scripts/arg.txt",
    )


@pytest.mark.parametrize(
    "argv",
    [["/usr/local/bin/x", "a/b"], ["python3", "scripts/x.py"]],
    ids=["absolute", "bare"],
)
def test_absolute_and_bare_commands_unchanged(argv: list[str], tmp_path: Path) -> None:
    assert resolve_argv(argv, tmp_path) == argv


@pytest.mark.usefixtures("head")
def test_nonzero_repair_parks_repair_failed_branch_untouched(
    git: ScriptedGit, cmds: ScriptedCommands, client: ClientConfig, arm: FakeArmScript
) -> None:
    cmds.set(REPAIR_ARGV[0], CommandOutcome(3, "boom: no parent", False))

    assert _dispatch(client) is None

    failure = _only_failure(D.REPAIR_FAILED)
    assert "exit 3" in failure["error"]
    _assert_untouched(git, arm)
    assert not _has(git, "commit")
    assert len(cmds.calls) == 1
    assert _LATCH.blocked(_KEY)


@pytest.mark.usefixtures("head", "arm")
def test_repair_timeout_parks_repair_failed(
    git: ScriptedGit, cmds: ScriptedCommands, client: ClientConfig
) -> None:
    cmds.set(REPAIR_ARGV[0], CommandOutcome(None, "still going", True))

    assert _dispatch(client) is None

    failure = _only_failure(D.REPAIR_FAILED)
    assert "timed out after 300s" in failure["error"]
    _assert_untouched(git)


@pytest.mark.usefixtures("head", "arm")
def test_repair_launch_oserror_parks_repair_failed(
    git: ScriptedGit, cmds: ScriptedCommands, client: ClientConfig
) -> None:
    cmds.set(REPAIR_ARGV[0], FileNotFoundError("no such file"))

    assert _dispatch(client) is None

    failure = _only_failure(D.REPAIR_FAILED)
    assert "could not run" in failure["error"]
    _assert_untouched(git)


def _write_in_scratch(client: ClientConfig, relpath: str, text: str) -> None:
    path = scratch_path(client, MECHANICAL_SPEC) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.usefixtures("head", "arm")
def test_repair_leaves_markers_parks_repair_incomplete(
    git: ScriptedGit, cmds: ScriptedCommands, client: ClientConfig
) -> None:
    markers = "a\n<<<<<<< HEAD\nb\n=======\nc\n>>>>>>> main\n"
    cmds.set(REPAIR_ARGV[0], _writes(client, CONFLICT, markers))

    assert _dispatch(client) is None

    failure = _only_failure(D.REPAIR_INCOMPLETE)
    assert CONFLICT in failure["error"]
    assert not _has(git, "commit")
    _assert_untouched(git)


def _writes(
    client: ClientConfig, relpath: str, text: str
) -> Callable[[CommandCall], CommandOutcome]:
    """A repair command that leaves *text* at *relpath* in the scratch tree."""

    def _run(_call: CommandCall) -> CommandOutcome:
        _write_in_scratch(client, relpath, text)
        return CommandOutcome(0, "", False)

    return _run


@pytest.mark.usefixtures("head", "arm")
def test_resolved_conflict_without_markers_is_committed(
    git: ScriptedGit, cmds: ScriptedCommands, client: ClientConfig
) -> None:
    cmds.set(REPAIR_ARGV[0], _writes(client, CONFLICT, "x = 1\n======= ok\n"))

    assert _dispatch(client) == TICKET

    assert _has(git, "commit", "--no-verify")


@pytest.mark.usefixtures("head", "arm", "cmds")
def test_repair_touches_non_glob_tracked_file_parks_repair_out_of_scope(
    git: ScriptedGit, client: ClientConfig
) -> None:
    git.rules = merge_happy_rules(
        status=f"UU {CONFLICT}\0 M {OUT_OF_GLOB}\0M  migrations/0002_main.py\0"
    )

    assert _dispatch(client) is None

    failure = _only_failure(D.REPAIR_OUT_OF_SCOPE)
    assert OUT_OF_GLOB in failure["error"]
    assert not _has(git, "--literal-pathspecs", "add")
    _assert_untouched(git)


@pytest.mark.usefixtures("head", "arm", "cmds")
def test_index_only_out_of_glob_merge_entries_are_not_parked_or_staged(
    git: ScriptedGit, client: ClientConfig
) -> None:
    # What a real merge leaves for files main changed outside the globs: they
    # are staged by the merge (second column clean), so the recipe leaves them
    # alone instead of reading them as the repair command's out-of-scope edit.
    git.rules = merge_happy_rules(
        status=(
            f"UU {CONFLICT}\0M  src/other.py\0A  src/new_in_main.py\0"
            "M  migrations/0002_main.py\0"
        )
    )

    assert _dispatch(client) == TICKET

    assert taken() != []
    assert failed() == []
    (added,) = [
        argv for argv in git.stripped() if argv[:2] == ("--literal-pathspecs", "add")
    ]
    assert set(added[4:]) == {CONFLICT}


@pytest.mark.usefixtures("head", "arm", "cmds")
def test_out_of_scope_paths_are_capped_in_the_error(
    git: ScriptedGit, client: ClientConfig
) -> None:
    many = "".join(f" M src/f{i:02d}.py\0" for i in range(25))
    git.rules = merge_happy_rules(status=f"UU {CONFLICT}\0{many}")

    assert _dispatch(client) is None

    failure = _only_failure(D.REPAIR_OUT_OF_SCOPE)
    assert "(+5 more)" in failure["error"]
    assert "src/f24.py" not in failure["error"]


@pytest.mark.usefixtures("head", "arm", "cmds")
def test_untracked_out_of_glob_file_is_ignored_not_staged(
    git: ScriptedGit, client: ClientConfig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="cw.reconcile.review_recipes")
    git.rules = merge_happy_rules(
        status=(
            f"UU {CONFLICT}\0?? migrations/0003_new.py\0?? build/out.log\0"
            " D migrations/0002_branch.py\0"
        )
    )

    assert _dispatch(client) == TICKET

    (added,) = [
        argv for argv in git.stripped() if argv[:2] == ("--literal-pathspecs", "add")
    ]
    assert added[:4] == ("--literal-pathspecs", "add", "-A", "--")
    assert set(added[4:]) == {
        CONFLICT,
        "migrations/0003_new.py",
        "migrations/0002_branch.py",
    }
    (ignored,) = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("mechanical_repair_untracked_ignored")
    ]
    assert "count=1" in ignored
    assert "build/out.log" in ignored


@pytest.mark.usefixtures("head", "arm")
@pytest.mark.parametrize(
    ("response", "needle"),
    [
        pytest.param(CommandOutcome(1, "two heads", False), "exit 1", id="nonzero"),
        pytest.param(
            CommandOutcome(None, "", True), "timed out after 120s", id="timeout"
        ),
        pytest.param(PermissionError("denied"), "could not run", id="oserror"),
    ],
)
def test_check_command_failure_parks_check_failed(
    git: ScriptedGit,
    cmds: ScriptedCommands,
    client: ClientConfig,
    response: CommandResponse,
    needle: str,
) -> None:
    cmds.set(VERIFY_ARGV[0], response)

    assert _dispatch(client) is None

    failure = _only_failure(D.CHECK_FAILED)
    assert needle in failure["error"]
    assert "verify command" in failure["error"]
    assert failure["evidence_snapshot"]["new_sha"] == NEW_SHA
    _assert_untouched(git)


def _by_range(
    before: GitResult, after: GitResult
) -> Callable[[tuple[str, ...]], GitResult]:
    def _respond(argv: tuple[str, ...]) -> GitResult:
        return before if MB_SHA in argv else after

    return _respond


@pytest.mark.usefixtures("head", "arm", "cmds")
def test_verify_fingerprint_mismatch_parks_verify_failed(
    git: ScriptedGit, client: ClientConfig
) -> None:
    git.set(
        ("--literal-pathspecs", "diff", "-U0"),
        _by_range(GitResult(stdout="+x = 1\n"), GitResult(stdout="+x = 2\n")),
    )

    assert _dispatch(client) is None

    failure = _only_failure(D.VERIFY_FAILED)
    assert failure["evidence_snapshot"]["new_sha"] == NEW_SHA
    assert "range-diff" in failure["error"]
    _assert_untouched(git)


@pytest.mark.usefixtures("head", "arm", "cmds")
def test_verify_pathset_mismatch_parks_verify_failed(
    git: ScriptedGit, client: ClientConfig
) -> None:
    git.set(
        ("diff", "--name-only", "-z", "--no-renames"),
        _by_range(
            GitResult(stdout=_z(OUT_OF_GLOB)),
            GitResult(stdout=_z(OUT_OF_GLOB, "src/other.py")),
        ),
    )

    assert _dispatch(client) is None

    failure = _only_failure(D.VERIFY_FAILED)
    assert "src/other.py" in failure["error"]


@pytest.mark.usefixtures("head", "arm", "cmds")
def test_verify_new_out_of_glob_path_parks_verify_failed(
    git: ScriptedGit, client: ClientConfig
) -> None:
    git.set(
        ("diff", "--name-only", "-z", "--no-renames"),
        _by_range(GitResult(stdout=_z(CONFLICT)), GitResult(stdout=_z(OUT_OF_GLOB))),
    )

    assert _dispatch(client) is None

    failure = _only_failure(D.VERIFY_FAILED)
    assert "branch [(none)]" in failure["error"]
    assert OUT_OF_GLOB in failure["error"]


def _ancestry(git: ScriptedGit, client: ClientConfig) -> list[tuple[str, ...]]:
    """The ancestry checks run in the scratch tree (the fence's run in the repo)."""
    scratch = scratch_path(client, MECHANICAL_SPEC)
    return [
        call.argv[-3:]
        for call in git.calls
        if "--is-ancestor" in call.argv and call.cwd == scratch
    ]


@pytest.mark.usefixtures("head", "arm", "cmds")
def test_repaired_head_descends_from_reviewed_tip_and_default_then_pushes(
    git: ScriptedGit, client: ClientConfig
) -> None:
    assert _dispatch(client) is not None

    assert ("--is-ancestor", EXPECTED_SHA, NEW_SHA) in _ancestry(git, client)
    assert ("--is-ancestor", DEFAULT_REF, NEW_SHA) in _ancestry(git, client)
    assert _has(git, "push")


@pytest.mark.usefixtures("head", "cmds")
@pytest.mark.parametrize(
    ("ancestor", "named"),
    [(EXPECTED_SHA, "the reviewed tip"), (DEFAULT_REF, "the default branch")],
    ids=["reviewed-tip", "default-branch"],
)
def test_repaired_head_that_does_not_descend_parks_verify_failed_without_push(
    git: ScriptedGit,
    client: ClientConfig,
    arm: FakeArmScript,
    ancestor: str,
    named: str,
) -> None:
    git.set(("merge-base", "--is-ancestor", ancestor), GitResult(1))

    assert _dispatch(client) is None

    failure = _only_failure(D.VERIFY_FAILED)
    assert named in failure["error"]
    assert "rewrote history" in failure["error"]
    assert failure["evidence_snapshot"]["new_sha"] == NEW_SHA
    _assert_untouched(git, arm)


@pytest.mark.usefixtures("head", "cmds")
def test_ancestry_check_git_error_parks_merge_failed_without_push(
    git: ScriptedGit, client: ClientConfig, arm: FakeArmScript
) -> None:
    git.set(("merge-base", "--is-ancestor"), GitResult(128, "", "fatal: bad object"))

    assert _dispatch(client) is None

    _only_failure(D.MERGE_FAILED)
    _assert_untouched(git, arm)


@pytest.mark.usefixtures("head", "arm", "cmds")
def test_nothing_left_to_stage_commits_without_add(
    git: ScriptedGit, client: ClientConfig
) -> None:
    git.rules = merge_happy_rules(status="M  migrations/0002_main.py\0")

    assert _dispatch(client) == TICKET

    assert not _has(git, "--literal-pathspecs", "add")
    assert _has(git, "commit", "--no-verify")


@pytest.mark.usefixtures("head", "arm", "cmds")
def test_no_out_of_glob_change_skips_the_fingerprint_diff(
    git: ScriptedGit, client: ClientConfig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="cw.reconcile.review_recipes")
    git.set(
        ("diff", "--name-only", "-z", "--no-renames"), GitResult(stdout=_z(CONFLICT))
    )

    assert _dispatch(client) == TICKET

    assert not _has(git, "--literal-pathspecs", "diff")
    assert any(
        "mechanical_repair_scope_verified" in r.getMessage()
        and "out_of_glob_paths=0" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.usefixtures("head", "arm", "cmds")
@pytest.mark.parametrize(
    "prefix",
    [
        ("diff", "--name-only", "-z", "--no-renames"),
        ("--literal-pathspecs", "diff", "-U0"),
    ],
    ids=["names", "fingerprint"],
)
def test_verify_git_error_parks_verify_failed(
    git: ScriptedGit, client: ClientConfig, prefix: tuple[str, ...]
) -> None:
    git.set(prefix, GitResult(128, "", "bad revision"))

    assert _dispatch(client) is None

    _only_failure(D.VERIFY_FAILED)


@pytest.mark.usefixtures("head", "arm", "cmds")
@pytest.mark.parametrize(
    "prefix",
    [
        ("commit",),
        ("rev-parse", "HEAD"),
        ("diff", "--name-only", "-z", "--diff-filter=U"),
        ("status", "--porcelain=v1"),
        ("--literal-pathspecs", "add"),
    ],
    ids=["commit", "rev-parse", "unmerged-list", "status", "add"],
)
def test_merge_commit_failure_parks_merge_failed(
    git: ScriptedGit, client: ClientConfig, prefix: tuple[str, ...]
) -> None:
    git.set(prefix, GitResult(128, "", "fatal: index.lock"))

    assert _dispatch(client) is None

    _only_failure(D.MERGE_FAILED)
    _assert_untouched(git)


@pytest.mark.usefixtures("head", "arm")
@pytest.mark.parametrize(
    "breaker",
    ["out_of_scope", "repair_failed", "incomplete", "check_failed", "success"],
)
def test_scratch_tree_removed_on_every_exit(
    git: ScriptedGit, cmds: ScriptedCommands, client: ClientConfig, breaker: str
) -> None:
    if breaker == "out_of_scope":
        git.rules = merge_happy_rules(unmerged=(CONFLICT, OUT_OF_GLOB))
    elif breaker == "repair_failed":
        cmds.set(REPAIR_ARGV[0], CommandOutcome(1, "", False))
    elif breaker == "incomplete":
        cmds.set(REPAIR_ARGV[0], _writes(client, CONFLICT, ">>>>>>> main\n"))
    elif breaker == "check_failed":
        cmds.set(VERIFY_ARGV[0], CommandOutcome(1, "", False))

    _dispatch(client)

    added = [i for i, a in enumerate(git.stripped()) if a[:2] == ("worktree", "add")]
    removed = [
        i for i, a in enumerate(git.stripped()) if a[:2] == ("worktree", "remove")
    ]
    assert added
    assert removed
    assert removed[-1] > added[-1]
    assert not scratch_path(client, MECHANICAL_SPEC).exists()


# --- push / finish ----------------------------------------------------------


def test_success_small_tier_pushes_with_lease_and_arms_pinned_to_new_sha(
    log: list[LogEntry],
    git: ScriptedGit,
    cmds: ScriptedCommands,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
) -> None:
    assert _dispatch(client) == TICKET

    (push,) = [argv for argv in git.stripped() if argv[0] == "push"]
    # The verified object is pushed by name, never the scratch tree's HEAD.
    assert push == (
        "push",
        "--no-verify",
        f"--force-with-lease=refs/heads/{BRANCH}:{EXPECTED_SHA}",
        "origin",
        f"{NEW_SHA}:refs/heads/{BRANCH}",
    )
    (merge,) = [argv for argv in git.stripped() if argv[0] == "merge"]
    assert merge == ("merge", "--no-commit", "--no-ff", DEFAULT_REF)
    merge_call = next(
        c for c in git.calls if "merge" in c.argv and "--no-commit" in c.argv
    )
    assert "user.name=cw-mechanical-repair" in merge_call.argv
    push_at = next(i for i, e in enumerate(log) if e[:2] == ("git", "push"))
    verify_at = max(i for i, e in enumerate(log) if e[0] == "cmd")
    assert verify_at < push_at
    taken_at = log.index(("event", "pr.action_taken"))
    assert push_at < taken_at
    (argv,) = arm.calls()
    assert argv == [
        "arm-automerge",
        PR_NUMBER,
        "--repo-path",
        str(scratch_path(client, MECHANICAL_SPEC)),
        "--head-sha",
        NEW_SHA,
    ]
    assert head.disarm_calls == 0


@pytest.mark.usefixtures("head")
def test_verify_command_that_moves_head_parks_verify_failed_without_push(
    log: list[LogEntry],
    git: ScriptedGit,
    cmds: ScriptedCommands,
    client: ClientConfig,
    arm: FakeArmScript,
) -> None:
    moved_sha = "d" * 40

    def _verify_commits(_call: CommandCall) -> CommandOutcome:
        # What a verify command that commits, amends or resets looks like.
        git.set(("rev-parse", "HEAD"), GitResult(stdout=f"{moved_sha}\n"))
        return CommandOutcome(0, "ok", False)

    cmds.set(VERIFY_ARGV[0], _verify_commits)

    assert _dispatch(client) is None

    failure = _only_failure(D.VERIFY_FAILED)
    assert "verify command moved HEAD" in failure["error"]
    assert NEW_SHA in failure["error"]
    assert moved_sha in failure["error"]
    assert failure["evidence_snapshot"]["new_sha"] == NEW_SHA
    _assert_untouched(git, arm)
    assert ("event", "pr.action_taken") not in log


@pytest.mark.usefixtures("head", "arm", "cmds", "git")
def test_taken_payload_has_recipe_conflict_kind_and_evidence_snapshot(
    client: ClientConfig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="cw.reconcile.review_recipes")

    assert _dispatch(client) == TICKET

    (done,) = taken()
    assert done["recipe"] == RECIPE_REPAIR_MECHANICAL_CONFLICT
    assert done["conflict_kind"] == "mechanical_paths_candidate"
    snapshot = done["evidence_snapshot"]
    assert set(snapshot) == _TAKEN_EVIDENCE_KEYS
    assert snapshot["conflicted_paths"] == [CONFLICT]
    assert snapshot["expected_sha"] == EXPECTED_SHA
    assert snapshot["new_sha"] == NEW_SHA
    (success,) = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("mechanical_repair_succeeded")
    ]
    assert success.endswith("conflicted_paths=1")
    finished = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("mechanical_repair_command_finished")
    ]
    assert len(finished) == 2
    assert all("--fix" not in m for m in finished)


@pytest.mark.usefixtures("head")
def test_out_of_scope_failed_payload_carries_conflicted_paths(
    git: ScriptedGit, client: ClientConfig
) -> None:
    git.rules = merge_happy_rules(conflicts=(OUT_OF_GLOB, CONFLICT))

    _dispatch(client)

    failure = _only_failure(D.OUT_OF_SCOPE_CONFLICT)
    assert failure["evidence_snapshot"]["conflicted_paths"] == [CONFLICT, OUT_OF_GLOB]
    assert failure["evidence_snapshot"]["new_sha"] is None


@pytest.mark.usefixtures("head", "arm", "git")
def test_failed_error_text_composition(
    cmds: ScriptedCommands, client: ClientConfig
) -> None:
    noisy = "x" * 400 + "\n  final   line  "
    cmds.set(REPAIR_ARGV[0], CommandOutcome(2, noisy, False))

    _dispatch(client)

    (failure,) = failed()
    error = failure["error"]
    assert error.startswith(f"repair command {REPAIR_ARGV[0]} exit 2: ")
    assert error.endswith("final line")
    assert "--fix" not in error
    assert len(error) < len(noisy)


def test_clean_mode_ends_unarmed_with_attention_page(
    git: ScriptedGit,
    cmds: ScriptedCommands,
    head: FakeGhHead,
    repair_workspace: Path,
    arm: FakeArmScript,
) -> None:
    client = make_client(
        repair_workspace,
        mechanical_conflict_repair=ENTRY,
        review={"convergence": "clean"},
    )

    assert _dispatch(client) == TICKET

    assert arm.calls() == []
    (page,) = attention()
    assert page["paused_status"] == "mechanical_repair_unarmed"
    assert page["review_tip_check"] == "stale"
    assert page["disposition"] == "unarmed_review_fence"
    assert page["recipe"] == RECIPE_REPAIR_MECHANICAL_CONFLICT
    assert "conflicted_paths" not in page
    assert len(taken()) == 1
    assert failed() == []


def test_clean_mode_ledger_converged_at_the_pre_repair_tip_leaves_head_unarmed(
    git: ScriptedGit,
    cmds: ScriptedCommands,
    head: FakeGhHead,
    repair_workspace: Path,
    arm: FakeArmScript,
    tmp_path: Path,
) -> None:
    """The repaired head stays subject to the review tip rule (#2917).

    The ledger is loaded and converged, but at the pre-repair tip, so the
    repaired merge head is not a reviewed head: the verdict is the ledger's
    own, not the "no ledger" shortcut the worktree-less test above takes.
    """
    worktree = tmp_path / "ticket-worktree"
    write_ledger(worktree, confirmed_ledger(sha=EXPECTED_SHA, ticket_id=TICKET))
    client = make_client(
        repair_workspace,
        mechanical_conflict_repair=ENTRY,
        review={"convergence": "clean"},
    )

    assert _dispatch(client, task=repair_task(worktree_path=worktree)) == TICKET

    assert _has(git, "push")
    assert arm.calls() == []
    (page,) = attention()
    assert page["paused_status"] == "mechanical_repair_unarmed"
    assert page["review_tip_check"] == "stale"
    breadcrumbs = page["breadcrumbs"]
    assert "verify-tip:" in breadcrumbs
    assert "no ledger" not in breadcrumbs
    assert "has no worktree" not in breadcrumbs
    assert page["new_sha"] == NEW_SHA
    assert len(taken()) == 1
    assert failed() == []


def test_large_tier_with_recorded_approval_non_clean_arms(
    git: ScriptedGit,
    cmds: ScriptedCommands,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
) -> None:
    """The merge keeps the reviewed tip as its first parent, so the fence's
    approval check still reads ``recorded`` and the PR is re-armed."""
    task = repair_task(
        computed_scope_tier="large",
        review_approver_kind="operator",
        review_approved_sha=EXPECTED_SHA,
    )
    git.set(("rev-list", "--no-merges"), GitResult())
    git.set(("rev-list", "--merges"), GitResult(stdout=f"{NEW_SHA}\n"))
    git.set(
        ("rev-list", "--parents"),
        GitResult(stdout=f"{NEW_SHA} {EXPECTED_SHA} {MB_SHA}\n"),
    )

    assert _dispatch(client, task=task) == TICKET

    assert len(arm.calls()) == 1
    assert attention() == []


@pytest.mark.usefixtures("head", "arm", "cmds")
def test_push_rejected_is_transient_not_saturating(
    git: ScriptedGit, client: ClientConfig
) -> None:
    git.set(("push",), GitResult(1, "", "stale info"))

    assert _dispatch(client) is None

    _only_failure(D.PUSH_REJECTED)
    assert _LATCH.attempts(_KEY) == 0


@pytest.mark.usefixtures("head", "arm", "cmds")
def test_push_timeout_is_outcome_unknown(
    git: ScriptedGit, client: ClientConfig
) -> None:
    git.set(("push",), "timeout")

    assert _dispatch(client) is None

    failure = _only_failure(D.PUSH_OUTCOME_UNKNOWN)
    assert "the push may have landed" in failure["error"]
    assert _LATCH.blocked(_KEY)


@pytest.mark.usefixtures("head", "arm", "cmds", "git")
def test_crash_after_push_is_post_push_failed(
    monkeypatch: pytest.MonkeyPatch, client: ClientConfig
) -> None:
    def _boom(*_a: object, **_kw: object) -> object:
        msg = "fence bug"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        "cw.reconcile.review_recipes.repair_engine.evaluate_repair_fence", _boom
    )

    assert _dispatch(client) is None

    assert len(taken()) == 1
    (failure,) = failed()
    assert failure["disposition"] == D.POST_PUSH_FAILED.value
    assert failure["evidence_snapshot"]["conflicted_paths"] == [CONFLICT]


@pytest.mark.usefixtures("arm", "cmds")
def test_armed_pr_disarm_failure_parks_before_push(
    monkeypatch: pytest.MonkeyPatch,
    log: list[LogEntry],
    git: ScriptedGit,
    client: ClientConfig,
) -> None:
    FakeGhHead(log, armed=True, disarm="none").install(monkeypatch)

    assert _dispatch(client) is None

    _only_failure(D.DISARM_FAILED)
    _assert_untouched(git)


@pytest.mark.usefixtures("head", "arm", "cmds", "git")
def test_unexpected_oserror_before_push_parks_merge_failed(
    monkeypatch: pytest.MonkeyPatch, client: ClientConfig
) -> None:
    def _boom(*_a: object, **_kw: object) -> None:
        msg = "disk gone"
        raise OSError(msg)

    monkeypatch.setattr(
        "cw.reconcile.review_recipes.mechanical_repair._scan_markers", _boom
    )

    assert _dispatch(client) is None

    failure = _only_failure(D.MERGE_FAILED)
    assert "OSError" in failure["error"]
    assert _LATCH.blocked(_KEY)


# --- tick wiring ------------------------------------------------------------


@pytest.fixture
def wired(tmp_config_dir: Path, repair_workspace: Path) -> None:
    lane = LaneConfig(
        name="default",
        review_recipes={
            RECIPE_REPAIR_CHANGELOG_CONFLICT: True,
            RECIPE_REPAIR_MECHANICAL_CONFLICT: True,
        },
    )
    write_clients_yaml(
        ClientSpec(
            "acme",
            repair_workspace,
            default_branch="main",
            worktree_base=repair_workspace.parent / "wt",
            lanes=[lane],
            extra={"mechanical_conflict_repair": ENTRY},
        )
    )
    save_dev_queue(DevQueueStore(tasks=[repair_task()]))


@pytest.mark.usefixtures("wired")
def test_both_recipes_enabled_only_changelog_job_queued_this_tick() -> None:
    cfg = config()
    jobs = DeferredReconcileJobs(review=DeferredReviewDispatch())

    run_review_recipes(
        config=cfg,
        jobs=jobs,
        repo_slugs=capture_review_repo_slugs(config=cfg, dispatching=True),
    )

    assert [job.label for job in jobs.post_lock] == [f"changelog_repair:acme:{TICKET}"]
    assert _LATCH.attempts(_KEY) == 0


@pytest.mark.usefixtures("wired", "head", "arm", "cmds", "git")
def test_saturated_changelog_latch_hands_the_row_to_the_mechanical_recipe() -> None:
    cfg = config()

    def _tick() -> DeferredReconcileJobs:
        jobs = DeferredReconcileJobs(review=DeferredReviewDispatch())
        run_review_recipes(
            config=cfg,
            jobs=jobs,
            repo_slugs=capture_review_repo_slugs(config=cfg, dispatching=True),
        )
        return jobs

    # Tick 1: the CHANGELOG recipe takes a mechanical-only conflict and parks
    # it (non_changelog_conflict), which saturates its latch. The mechanical
    # recipe was excluded for the tick, so its latch is untouched.
    first = _tick()
    assert [job.label for job in first.post_lock] == [f"changelog_repair:acme:{TICKET}"]
    assert _CHANGELOG_LATCH.attempts(_KEY) == 1
    assert _LATCH.attempts(_KEY) == 0
    run_post_lock_jobs(first)
    (parked,) = failed()
    assert parked["disposition"] == D.NON_CHANGELOG_CONFLICT.value
    assert parked["recipe"] == RECIPE_REPAIR_CHANGELOG_CONFLICT
    assert _CHANGELOG_LATCH.blocked(_KEY)
    assert _LATCH.attempts(_KEY) == 0

    # Tick 2: the row is still CONFLICTING and the CHANGELOG latch is spent,
    # so the mechanical recipe gets its turn (one attempt, no exclusion).
    second = _tick()
    assert [job.label for job in second.post_lock] == [
        f"mechanical_repair:acme:{TICKET}"
    ]
    assert _LATCH.attempts(_KEY) == 1
    assert _CHANGELOG_LATCH.attempts(_KEY) == _MAX_ATTEMPTS


@pytest.mark.usefixtures("wired")
def test_mechanical_job_label_and_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    store = load_dev_queue()
    store.tasks[0].review_recipes = {RECIPE_REPAIR_CHANGELOG_CONFLICT: False}
    save_dev_queue(store)

    def _boom(_job: object) -> str | None:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr("cw.reconcile.review_recipes.core.dispatch_repair", _boom)
    cfg = config()
    jobs = DeferredReconcileJobs(review=DeferredReviewDispatch())
    run_review_recipes(
        config=cfg,
        jobs=jobs,
        repo_slugs=capture_review_repo_slugs(config=cfg, dispatching=True),
    )

    assert [job.label for job in jobs.post_lock] == [f"mechanical_repair:acme:{TICKET}"]
    run_post_lock_jobs(jobs)
    (failure,) = failed()
    assert failure["error"] == "RuntimeError: boom"
    assert failure["recipe"] == RECIPE_REPAIR_MECHANICAL_CONFLICT


@pytest.mark.usefixtures("wired")
def test_review_sink_none_never_fires() -> None:
    jobs = DeferredReconcileJobs()

    run_review_recipes(config=config(), jobs=jobs)

    assert jobs.post_lock == []
    assert _LATCH.attempts(_KEY) == 0


@pytest.mark.usefixtures("wired")
def test_slug_prepass_includes_mechanical_candidates(repair_workspace: Path) -> None:
    store = load_dev_queue()
    store.tasks[0].review_recipes = {RECIPE_REPAIR_CHANGELOG_CONFLICT: False}
    save_dev_queue(store)

    slugs = capture_review_repo_slugs(config=config(), dispatching=True)

    assert repair_workspace in slugs


def test_recipe_not_in_fired_at_maps() -> None:
    assert RECIPE_REPAIR_MECHANICAL_CONFLICT not in RECIPE_ATTENTION_STATES
    assert RECIPE_REPAIR_MECHANICAL_CONFLICT not in RECIPE_FIRED_AT_GETTERS


# --- docs -------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_docs_mention_entry_dispositions_and_paused_status() -> None:
    reference = (_REPO_ROOT / "config/CONFIG_REFERENCE.md").read_text(encoding="utf-8")
    runbook = (_REPO_ROOT / "docs/dispatch-runbook.md").read_text(encoding="utf-8")
    events = (_REPO_ROOT / "docs/events.md").read_text(encoding="utf-8")

    assert "mechanical_conflict_repair" in reference
    assert "repair_mechanical_conflict" in reference
    assert "mechanical_conflict_repair" in runbook
    assert "`mechanical_repair_unarmed`" in events
    # A relative argv[0] is read from the checked-out working tree (#2917).
    assert "currently checked-out working tree" in " ".join(runbook.split())
    assert "currently checked-out working tree" in reference
    # With both recipes enabled the CHANGELOG recipe takes the row first.
    flat_runbook = " ".join(runbook.split())
    assert "one `non_changelog_conflict` for such a row" in flat_runbook
    assert "one `non_changelog_conflict`" in " ".join(events.split())
    # The post-lock job holds the whole dispatch tick against the watchdog floor.
    assert "whole dispatch tick" in flat_runbook
    assert "the whole dispatch tick waits on it" in " ".join(reference.split())
    # verify_failed has three causes, and conflicted_paths is null for a
    # conflict_unexplained raised inside the derivation.
    assert "does not descend from" in flat_runbook
    assert "does not descend from" in " ".join(events.split())
    assert "moved HEAD" in flat_runbook
    assert "`conflict_unexplained` raised by a git failure" in " ".join(events.split())
