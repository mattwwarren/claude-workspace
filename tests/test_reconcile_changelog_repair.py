"""Tests for the repair_changelog_conflict review recipe (#2771).

Detect and act run against the real dev queue; the post-lock dispatch runs
with ``ScriptedGit`` (for ``run_git``), ``FakeGhHead`` (for the gh head read
and disarm) and ``FakeArmScript`` (the arm subprocess). Real-git coverage
lives in ``tests/test_changelog_repair_real_git.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from cw.changelog_rebuild import ChangelogRepairDisposition as D
from cw.config import sessions_lock
from cw.dev_queue import load_dev_queue, precheck_requeue, save_dev_queue
from cw.events import read_events
from cw.exceptions import CwError, RequeueStageError
from cw.gh import PrHead
from cw.models import (
    ClientConfig,
    DevQueueStore,
    LaneConfig,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile.deferred import DeferredReconcileJobs, run_post_lock_jobs
from cw.reconcile.review_recipes import (
    RECIPE_ATTENTION_STATES,
    RECIPE_FIRED_AT_GETTERS,
    RECIPE_REPAIR_CHANGELOG_CONFLICT,
    DeferredReviewDispatch,
    run_review_recipes,
)
from cw.reconcile.review_recipes import changelog_repair as repair
from cw.reconcile.review_recipes import changelog_repair_git as git_mod
from cw.reconcile.review_recipes._shared import RepoSlugs
from cw.reconcile.review_recipes.auto_fix_ci import _auto_fix_ci_guard_target
from cw.reconcile.review_recipes.changelog_repair import (
    _LATCH,
    _MAX_ATTEMPTS,
    _act_repair_changelog_conflict,
    _detect_repair_changelog_conflict,
    _dispatch_changelog_repair,
)
from cw.reconcile.review_recipes.changelog_repair_git import _MUTATING_FLAGS
from cw.reconcile.review_recipes.core import capture_review_repo_slugs
from tests._changelog_repair_helpers import (
    BRANCH,
    CL_BASE,
    CL_MAIN,
    DEFAULT_REF,
    DISARM_NONE,
    DISARM_NONZERO_STILL_ARMED,
    DISARM_NONZERO_UNARMED,
    DISARM_OK,
    DISARM_ZERO_STILL_ARMED,
    EXPECTED_SHA,
    MB_SHA,
    NEW_SHA,
    OSERROR,
    PR_NUMBER,
    PR_URL,
    TICKET,
    TIMEOUT,
    FakeArmScript,
    FakeGhHead,
    GitResult,
    LogEntry,
    ScriptedGit,
    make_client,
    make_head,
    make_repair_job,
    repair_task,
    scratch_path,
    strip_flags,
    track_events,
)
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._reconcile_helpers import capture_repo_slugs
from tests.conftest import init_repo_with_remote
from tests.test_pr_hydrate import _pr_state
from tests.test_reconcile_gate_recipes import _client_with_lanes

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _config(**kwargs: Any) -> OrchestratorConfig:
    kwargs.setdefault("review_recipes_enabled", True)
    return OrchestratorConfig(**kwargs)


def _enabling_clients_for(*recipes: str) -> dict[str, ClientConfig]:
    lane = LaneConfig(name="default", review_recipes=dict.fromkeys(recipes, True))
    return {"acme": _client_with_lanes(lane)}


def _payloads(event_type: OrchestratorEventType) -> list[dict[str, Any]]:
    return [dict(e.payload) for e in read_events(event_types=[event_type])]


def _failed() -> list[dict[str, Any]]:
    return _payloads(OrchestratorEventType.PR_ACTION_FAILED)


def _taken() -> list[dict[str, Any]]:
    return _payloads(OrchestratorEventType.PR_ACTION_TAKEN)


def _attention() -> list[dict[str, Any]]:
    return _payloads(OrchestratorEventType.SESSION_NEEDS_ATTENTION)


# --- detect -----------------------------------------------------------------


def _detect(task: Any, clients: Any = None, config: Any = None) -> list[Any]:
    return _detect_repair_changelog_conflict(
        [task],
        clients=clients or _enabling_clients_for(RECIPE_REPAIR_CHANGELOG_CONFLICT),
        config=config or _config(),
    )


def test_detect_fires_for_conflicting_dirty_completed_merge_blocked() -> None:
    cands = _detect(repair_task())

    assert len(cands) == 1
    assert cands[0].recipe == RECIPE_REPAIR_CHANGELOG_CONFLICT
    assert cands[0].attention_state == "merge_blocked"


@pytest.mark.parametrize(
    "task",
    [
        pytest.param(
            repair_task(
                pr_state=_pr_state(
                    mergeable="MERGEABLE",
                    merge_state_status="BEHIND",
                    attention_state="merge_blocked",
                )
            ),
            id="behind",
        ),
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
        pytest.param(repair_task(status=QueueItemStatus.RUNNING), id="running"),
        pytest.param(repair_task(status=QueueItemStatus.PENDING), id="pending"),
        pytest.param(
            repair_task(
                pr_state=_pr_state(
                    mergeable="CONFLICTING",
                    merge_state_status="DIRTY",
                    attention_state="ci_failing",
                )
            ),
            id="other-attention-state",
        ),
    ],
)
def test_detect_skips_non_candidates(task: Any) -> None:
    assert _detect(task) == []


def test_detect_respects_the_master_switch() -> None:
    assert _detect(repair_task(), config=_config(review_recipes_enabled=False)) == []


def test_detect_is_default_off() -> None:
    clients = {"acme": _client_with_lanes(LaneConfig(name="default"))}

    assert _detect(repair_task(), clients=clients) == []


def test_detect_enabled_via_ticket_map() -> None:
    clients = {"acme": _client_with_lanes(LaneConfig(name="default"))}
    task = repair_task(review_recipes={RECIPE_REPAIR_CHANGELOG_CONFLICT: True})

    assert len(_detect(task, clients=clients)) == 1


# --- act --------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "client"
    path.mkdir()
    return path


@pytest.fixture
def act_clients(workspace: Path) -> dict[str, ClientConfig]:
    lane = LaneConfig(
        name="default", review_recipes={RECIPE_REPAIR_CHANGELOG_CONFLICT: True}
    )
    return {"acme": make_client(workspace, lanes=[lane])}


def _act(
    clients: dict[str, ClientConfig],
    *,
    slugs: RepoSlugs | None = None,
) -> list[Any]:
    store = load_dev_queue()
    candidates = _detect_repair_changelog_conflict(
        store.tasks, clients=clients, config=_config()
    )
    captured = (
        slugs if slugs is not None else capture_repo_slugs(candidates, clients=clients)
    )
    with sessions_lock():
        return _act_repair_changelog_conflict(
            candidates,
            clients=clients,
            repo_slugs=captured,
            guard_target=_auto_fix_ci_guard_target,
        )


def test_act_returns_one_job_and_latches_a_second_tick(
    act_clients: dict[str, ClientConfig],
) -> None:
    save_dev_queue(DevQueueStore(tasks=[repair_task()]))

    for _ in range(_MAX_ATTEMPTS):
        assert len(_act(act_clients)) == 1

    assert _act(act_clients) == []


def test_act_emits_nothing_and_saves_nothing(
    act_clients: dict[str, ClientConfig],
) -> None:
    save_dev_queue(DevQueueStore(tasks=[repair_task()]))
    before = load_dev_queue().model_dump_json()

    jobs = _act(act_clients)

    assert len(jobs) == 1
    assert not hasattr(repair, "save_dev_queue")
    assert load_dev_queue().model_dump_json() == before
    assert read_events() == []


def test_act_job_carries_session_conflict_kind_and_fence(
    act_clients: dict[str, ClientConfig], tmp_path: Path
) -> None:
    task = repair_task(
        scope_hint="large",
        computed_scope_tier="small",
        review_approver_kind="operator",
        review_approved_sha="abc1234",
        worktree_path=tmp_path / "wt-row",
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    (job,) = _act(act_clients)

    assert job.session_id == task.session_id
    assert job.payload_base["session_id"] == task.session_id
    assert job.payload_base["conflict_kind"] == "changelog_only_candidate"
    assert job.payload_base["evidence_snapshot"] == {
        "merge_state_status": "DIRTY",
        "mergeable": "CONFLICTING",
        "expected_sha": None,
        "new_sha": None,
        "auto_merge_was_armed": None,
    }
    assert job.fence.tier == "large"
    assert job.fence.review_approver_kind == "operator"
    assert job.fence.review_approved_sha == "abc1234"
    assert job.fence.clean_mode is False
    assert job.fence.ledger_path == tmp_path / "wt-row/.cw/review-convergence.json"


def test_act_is_bounded_to_one_job_per_tick(
    act_clients: dict[str, ClientConfig],
) -> None:
    save_dev_queue(DevQueueStore(tasks=[repair_task(), repair_task(ticket_id="GEN-2")]))

    assert len(_act(act_clients)) == 1
    assert len(_act(act_clients)) == 1


def test_latch_is_cleared_when_the_row_leaves_the_episode(
    act_clients: dict[str, ClientConfig],
) -> None:
    save_dev_queue(DevQueueStore(tasks=[repair_task()]))
    for _ in range(_MAX_ATTEMPTS):
        _act(act_clients)
    store = load_dev_queue()
    store.tasks[0].pr_state = _pr_state(attention_state="ready_to_approve")
    save_dev_queue(store)
    _act(act_clients)
    store.tasks[0].pr_state = repair_task().pr_state
    save_dev_queue(store)

    assert len(_act(act_clients)) == 1


def test_act_cross_repo_mismatch_emits_failed(
    act_clients: dict[str, ClientConfig], workspace: Path
) -> None:
    init_repo_with_remote(workspace, "https://github.com/other/thing.git")
    save_dev_queue(DevQueueStore(tasks=[repair_task()]))

    assert _act(act_clients) == []

    (failed,) = _failed()
    assert "repo mismatch" in failed["error"]
    assert failed["recipe"] == RECIPE_REPAIR_CHANGELOG_CONFLICT


def test_act_slug_miss_defers_without_bumping_the_latch(
    act_clients: dict[str, ClientConfig],
) -> None:
    save_dev_queue(DevQueueStore(tasks=[repair_task()]))

    assert _act(act_clients, slugs=RepoSlugs()) == []

    assert read_events() == []
    assert _LATCH.attempts(("acme", TICKET, PR_URL)) == 0


def test_act_skips_a_vanished_row(act_clients: dict[str, ClientConfig]) -> None:
    save_dev_queue(DevQueueStore(tasks=[repair_task()]))
    candidates = _detect_repair_changelog_conflict(
        load_dev_queue().tasks, clients=act_clients, config=_config()
    )
    save_dev_queue(DevQueueStore(tasks=[]))

    assert (
        _act_repair_changelog_conflict(
            candidates,
            clients=act_clients,
            repo_slugs=RepoSlugs(),
            guard_target=_auto_fix_ci_guard_target,
        )
        == []
    )


# --- wiring -----------------------------------------------------------------


@pytest.fixture
def wired(tmp_config_dir: Path, workspace: Path) -> None:
    write_clients_yaml(
        ClientSpec(
            "acme",
            workspace,
            default_branch="main",
            worktree_base=workspace.parent / "wt",
        )
    )
    task = repair_task(review_recipes={RECIPE_REPAIR_CHANGELOG_CONFLICT: True})
    save_dev_queue(DevQueueStore(tasks=[task]))


@pytest.mark.usefixtures("wired")
def test_run_review_recipes_appends_a_labelled_post_lock_job() -> None:
    config = _config()
    jobs = DeferredReconcileJobs(review=DeferredReviewDispatch())
    slugs = capture_review_repo_slugs(config=config, dispatching=True)

    run_review_recipes(config=config, jobs=jobs, repo_slugs=slugs)

    labels = [job.label for job in jobs.post_lock]
    assert labels == [f"changelog_repair:acme:{TICKET}"]


@pytest.mark.usefixtures("wired")
def test_run_review_recipes_without_review_sink_does_nothing() -> None:
    jobs = DeferredReconcileJobs()

    run_review_recipes(config=_config(), jobs=jobs)

    assert jobs.post_lock == []
    assert _LATCH.attempts(("acme", TICKET, PR_URL)) == 0


@pytest.mark.usefixtures("wired")
def test_crashing_job_becomes_pr_action_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_job: object) -> str | None:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        "cw.reconcile.review_recipes.core._dispatch_changelog_repair", _boom
    )
    config = _config()
    jobs = DeferredReconcileJobs(review=DeferredReviewDispatch())
    run_review_recipes(
        config=config,
        jobs=jobs,
        repo_slugs=capture_review_repo_slugs(config=config, dispatching=True),
    )

    run_post_lock_jobs(jobs)

    (failed,) = _failed()
    assert failed["error"] == "RuntimeError: boom"


@pytest.mark.usefixtures("wired")
def test_slug_pre_pass_captures_the_repair_candidates_dir(workspace: Path) -> None:
    slugs = capture_review_repo_slugs(config=_config(), dispatching=True)

    assert workspace in slugs


# --- dispatch fixtures ------------------------------------------------------


@pytest.fixture
def log() -> list[LogEntry]:
    return []


@pytest.fixture
def git(monkeypatch: pytest.MonkeyPatch, log: list[LogEntry]) -> ScriptedGit:
    return ScriptedGit(log).install(monkeypatch)


@pytest.fixture
def head(monkeypatch: pytest.MonkeyPatch, log: list[LogEntry]) -> FakeGhHead:
    return FakeGhHead(log).install(monkeypatch)


@pytest.fixture(autouse=True)
def _events(monkeypatch: pytest.MonkeyPatch, log: list[LogEntry]) -> None:
    track_events(monkeypatch, log)


@pytest.fixture
def client(workspace: Path) -> ClientConfig:
    return make_client(workspace)


@pytest.fixture
def arm(workspace: Path) -> FakeArmScript:
    return FakeArmScript(workspace)


def _dispatch(client: ClientConfig, **kwargs: Any) -> str | None:
    return _dispatch_changelog_repair(make_repair_job(client, **kwargs))


def _verbs(git: ScriptedGit) -> list[str]:
    return [argv[0] for argv in git.stripped()]


def _no_rewrite(git: ScriptedGit) -> None:
    for argv in git.stripped():
        assert argv[0] not in {"push", "rebase"}, argv
        assert argv[:2] != ("worktree", "add"), argv


def _only_failure(disposition: D) -> dict[str, Any]:
    assert _taken() == []
    (failed,) = _failed()
    assert failed["disposition"] == disposition.value
    assert failed["conflict_kind"] == "changelog_only_candidate"
    return failed


_KEY = ("acme", TICKET, PR_URL)


# --- dispatch: pre-push failures --------------------------------------------


@pytest.mark.usefixtures("head")
def test_fetch_failure(git: ScriptedGit, client: ClientConfig) -> None:
    git.set(("fetch", "origin"), GitResult(1, "", "no network"))

    assert _dispatch(client) is None

    failed = _only_failure(D.FETCH_FAILED)
    assert failed["evidence_snapshot"]["expected_sha"] is None
    _no_rewrite(git)
    assert _LATCH.attempts(_KEY) == 0


@pytest.mark.usefixtures("head")
def test_fetch_timeout(git: ScriptedGit, client: ClientConfig) -> None:
    git.set(("fetch", "origin"), TIMEOUT)

    _dispatch(client)

    _only_failure(D.FETCH_FAILED)


@pytest.mark.usefixtures("git")
@pytest.mark.parametrize(
    "reads",
    [
        {0: None},
        {0: make_head("dev/other", EXPECTED_SHA)},
        {0: make_head(BRANCH, NEW_SHA)},
        {0: make_head(base_ref_name="develop")},
        {0: make_head(state="CLOSED")},
    ],
)
def test_pr_head_mismatch_at_step_3(
    monkeypatch: pytest.MonkeyPatch,
    log: list[LogEntry],
    client: ClientConfig,
    reads: dict[int, PrHead | None],
) -> None:
    FakeGhHead(log, reads=reads).install(monkeypatch)

    _dispatch(client)

    failed = _only_failure(D.PR_HEAD_MISMATCH)
    assert failed["evidence_snapshot"]["expected_sha"] == EXPECTED_SHA


@pytest.mark.usefixtures("head")
def test_unparseable_pr_url(git: ScriptedGit, client: ClientConfig) -> None:
    task = repair_task(pr_url="https://example.com/not-a-pr")

    _dispatch(client, task=task)

    _only_failure(D.PR_HEAD_MISMATCH)
    assert git.calls == []


@pytest.mark.usefixtures("head", "arm")
def test_non_changelog_conflict_names_the_files(
    git: ScriptedGit, client: ClientConfig
) -> None:
    git.set(("merge-tree",), GitResult(1, "tree0\nsrc/app.py\nCHANGELOG.md\n"))

    _dispatch(client)

    failed = _only_failure(D.NON_CHANGELOG_CONFLICT)
    assert "src/app.py" in failed["error"]
    _no_rewrite(git)
    assert _LATCH.attempts(_KEY) == _MAX_ATTEMPTS


@pytest.mark.usefixtures("head", "arm")
def test_conflict_unexplained(git: ScriptedGit, client: ClientConfig) -> None:
    git.set(("diff", "--name-only"), GitResult())

    _dispatch(client)

    _only_failure(D.CONFLICT_UNEXPLAINED)
    _no_rewrite(git)


@pytest.mark.usefixtures("head", "arm")
@pytest.mark.parametrize(
    ("prefix", "response"),
    [
        (("merge-base", DEFAULT_REF), GitResult(1)),
        (("merge-tree",), GitResult(128, "", "fatal")),
        (("diff", "--name-only"), GitResult(128)),
    ],
)
def test_detector_git_errors_are_unexplained(
    git: ScriptedGit,
    client: ClientConfig,
    prefix: tuple[str, ...],
    response: GitResult,
) -> None:
    git.set(prefix, response)

    _dispatch(client)

    _only_failure(D.CONFLICT_UNEXPLAINED)
    _no_rewrite(git)


@pytest.mark.usefixtures("head", "arm")
@pytest.mark.parametrize(
    ("branch_text", "disposition"),
    [
        (CL_BASE.replace("## [Unreleased]", "## [Pending]"), D.CHANGELOG_UNPARSEABLE),
        (
            CL_BASE.replace("- Initial (#0).", "- Edited."),
            D.CHANGELOG_RELEASED_SECTION_TOUCHED,
        ),
        (
            CL_BASE.replace("- Beta fix (#2).", "- Beta, reworded."),
            D.CHANGELOG_AMBIGUOUS,
        ),
    ],
)
def test_rebuild_errors_park_before_any_worktree(
    git: ScriptedGit, client: ClientConfig, branch_text: str, disposition: D
) -> None:
    git.set(("show", f"{EXPECTED_SHA}:CHANGELOG.md"), GitResult(stdout=branch_text))

    _dispatch(client)

    _only_failure(disposition)
    _no_rewrite(git)


@pytest.mark.usefixtures("head", "arm")
def test_unreadable_blob_is_unparseable(git: ScriptedGit, client: ClientConfig) -> None:
    git.set(("show", f"{MB_SHA}:CHANGELOG.md"), GitResult(128))

    _dispatch(client)

    _only_failure(D.CHANGELOG_UNPARSEABLE)
    _no_rewrite(git)


@pytest.mark.usefixtures("head")
def test_arm_script_missing_parks_before_any_worktree(
    git: ScriptedGit, client: ClientConfig
) -> None:
    _dispatch(client)

    _only_failure(D.ARM_SCRIPT_MISSING)
    _no_rewrite(git)
    assert ("worktree", "prune") not in git.stripped()


@pytest.mark.usefixtures("head", "arm")
def test_scratch_worktree_failure_is_transient_and_cleaned(
    git: ScriptedGit, client: ClientConfig
) -> None:
    git.set(("worktree", "add"), GitResult(128, "", "already exists"))

    _dispatch(client)

    _only_failure(D.SCRATCH_WORKTREE_FAILED)
    assert git.stripped().count(("worktree", "prune")) == 2
    assert _LATCH.attempts(_KEY) == 0


@pytest.mark.usefixtures("head", "arm")
def test_rebase_failure_aborts(git: ScriptedGit, client: ClientConfig) -> None:
    git.set(("rebase", DEFAULT_REF), GitResult(1, "", "conflict"))
    git.set(("diff", "--name-only", "--diff-filter=U"), GitResult())

    _dispatch(client)

    _only_failure(D.REBASE_FAILED)
    assert ("rebase", "--abort") in git.stripped()
    assert "push" not in _verbs(git)


@pytest.mark.usefixtures("head", "arm")
def test_rebase_timeout_aborts(git: ScriptedGit, client: ClientConfig) -> None:
    git.set(("rebase", DEFAULT_REF), TIMEOUT)

    _dispatch(client)

    _only_failure(D.REBASE_FAILED)
    assert ("rebase", "--abort") in git.stripped()


@pytest.mark.usefixtures("head", "arm")
def test_rebase_stopping_on_another_file_is_non_changelog(
    git: ScriptedGit, client: ClientConfig
) -> None:
    git.set(("rebase", DEFAULT_REF), GitResult(1))
    git.set(("diff", "--name-only", "--diff-filter=U"), GitResult(stdout="src/a.py\n"))

    _dispatch(client)

    _only_failure(D.NON_CHANGELOG_CONFLICT)
    assert ("rebase", "--abort") in git.stripped()


@pytest.mark.usefixtures("head", "arm")
def test_rebase_stopping_on_changelog_takes_mains_text_and_continues(
    git: ScriptedGit, client: ClientConfig
) -> None:
    git.set(("rebase", DEFAULT_REF), GitResult(1))
    git.set(
        ("diff", "--name-only", "--diff-filter=U"), GitResult(stdout="CHANGELOG.md\n")
    )

    assert _dispatch(client) == TICKET

    verbs = git.stripped()
    assert ("rebase", "--continue") in verbs
    assert ("add", "CHANGELOG.md") in verbs
    assert "checkout" not in _verbs(git)


@pytest.mark.usefixtures("head", "arm")
def test_rebase_cap_overrun_fails(git: ScriptedGit, client: ClientConfig) -> None:
    git.set(("rebase",), GitResult(1))
    git.set(
        ("diff", "--name-only", "--diff-filter=U"), GitResult(stdout="CHANGELOG.md\n")
    )

    _dispatch(client)

    _only_failure(D.REBASE_FAILED)
    assert git.stripped().count(("rebase", "--continue")) == 2


@pytest.mark.usefixtures("head", "arm")
def test_verify_failure_attaches_range_diff(
    git: ScriptedGit, client: ClientConfig
) -> None:
    outputs = iter(["+x = 1\n", "+x = 2\n"])
    git.set(("diff", "-U0"), lambda _argv: GitResult(stdout=next(outputs)))
    git.set(("range-diff",), GitResult(stdout="1: abc ! 1: def gamma\n"))

    _dispatch(client)

    failed = _only_failure(D.VERIFY_FAILED)
    assert "range-diff" in failed["error"]
    assert failed["evidence_snapshot"]["new_sha"] == NEW_SHA
    assert "push" not in _verbs(git)


@pytest.mark.usefixtures("head", "arm")
def test_verify_diff_error_fails(git: ScriptedGit, client: ClientConfig) -> None:
    git.set(("diff", "-U0"), GitResult(128))

    _dispatch(client)

    _only_failure(D.VERIFY_FAILED)


def _plant_gate(client: ClientConfig, body: str) -> Path:
    """The client workspace's freeze-gate script (never the scratch tree's)."""
    gate = client.workspace_path / ".claude" / "scripts" / "check_changelog_frozen.py"
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.write_text(body, encoding="utf-8")
    return gate


@pytest.mark.usefixtures("head", "arm")
def test_gate_failure(git: ScriptedGit, client: ClientConfig) -> None:
    _plant_gate(client, "import sys\nsys.stderr.write('frozen')\nsys.exit(1)\n")

    _dispatch(client)

    failed = _only_failure(D.GATE_FAILED)
    assert "frozen" in failed["error"]
    assert "push" not in _verbs(git)


_GATE_RECORDING_BODY = (
    "import sys\nfrom pathlib import Path\n"
    "Path(__file__).with_suffix('.argv').write_text(' '.join(sys.argv[1:]))\n"
)


@pytest.mark.usefixtures("head", "arm")
def test_gate_runs_the_client_script_with_require_tags(
    git: ScriptedGit, client: ClientConfig
) -> None:
    gate = _plant_gate(client, _GATE_RECORDING_BODY)

    assert _dispatch(client) == TICKET

    argv = gate.with_suffix(".argv").read_text(encoding="utf-8").split()
    assert "--require-tags" in argv
    assert argv[argv.index("--changelog") + 1] == str(
        scratch_path(client) / "CHANGELOG.md"
    )
    assert ("fetch", "--tags") in git.stripped()


@pytest.mark.usefixtures("head", "arm")
def test_gate_script_comes_from_the_client_not_the_branch(
    git: ScriptedGit, client: ClientConfig
) -> None:
    _plant_gate(client, "import sys\nsys.stderr.write('client gate')\nsys.exit(1)\n")
    scratch = scratch_path(client)

    def _add(argv: tuple[str, ...]) -> GitResult:
        branch_gate = scratch / ".claude" / "scripts" / "check_changelog_frozen.py"
        branch_gate.parent.mkdir(parents=True, exist_ok=True)
        branch_gate.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        return GitResult()

    git.set(("worktree", "add"), _add)

    _dispatch(client)

    failed = _only_failure(D.GATE_FAILED)
    assert "client gate" in failed["error"]


@pytest.mark.usefixtures("head", "arm")
def test_absent_gate_script_is_a_logged_skip(
    git: ScriptedGit, client: ClientConfig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="cw.reconcile.review_recipes")

    assert _dispatch(client) == TICKET

    skipped = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("changelog_repair_gate_skipped")
    ]
    assert len(skipped) == 1
    assert "check_changelog_frozen.py" in skipped[0]
    assert ("fetch", "--tags") not in git.stripped()


@pytest.mark.usefixtures("head", "arm")
@pytest.mark.parametrize("response", [TIMEOUT, GitResult(1, "", "no network")])
def test_failed_tag_fetch_fails_the_gate_closed(
    git: ScriptedGit, client: ClientConfig, response: Any
) -> None:
    _plant_gate(client, "import sys\nsys.exit(0)\n")
    git.set(("fetch", "--tags"), response)

    _dispatch(client)

    _only_failure(D.GATE_FAILED)
    assert "push" not in _verbs(git)


@pytest.mark.usefixtures("head", "arm")
def test_push_rejected_is_transient(git: ScriptedGit, client: ClientConfig) -> None:
    git.set(("push",), GitResult(1, "", "stale info"))

    _dispatch(client)

    failed = _only_failure(D.PUSH_REJECTED)
    assert failed["evidence_snapshot"]["expected_sha"] == EXPECTED_SHA
    assert failed["evidence_snapshot"]["new_sha"] == NEW_SHA
    assert _LATCH.attempts(_KEY) == 0


@pytest.mark.usefixtures("head", "arm")
def test_push_timeout_is_outcome_unknown_and_saturates(
    git: ScriptedGit, client: ClientConfig
) -> None:
    git.set(("push",), TIMEOUT)

    _dispatch(client)

    failed = _only_failure(D.PUSH_OUTCOME_UNKNOWN)
    assert EXPECTED_SHA in failed["error"]
    assert _LATCH.attempts(_KEY) == _MAX_ATTEMPTS


# --- dispatch: disarm -------------------------------------------------------


@pytest.mark.usefixtures("arm")
@pytest.mark.parametrize(
    ("mode", "unreadable"),
    [
        (DISARM_NONZERO_STILL_ARMED, False),
        (DISARM_NONE, False),
        (DISARM_ZERO_STILL_ARMED, False),
        (DISARM_NONZERO_UNARMED, True),
    ],
)
def test_disarm_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    log: list[LogEntry],
    git: ScriptedGit,
    client: ClientConfig,
    mode: str,
    unreadable: bool,
) -> None:
    FakeGhHead(
        log, armed=True, disarm=mode, unreadable_after_disarm=unreadable
    ).install(monkeypatch)

    _dispatch(client)

    failed = _only_failure(D.DISARM_FAILED)
    assert failed["evidence_snapshot"]["expected_sha"] == EXPECTED_SHA
    assert failed["evidence_snapshot"]["auto_merge_was_armed"] is True
    disarm_at = log.index(("gh", "disarm"))
    after = [e for e in log[disarm_at:] if e[0] == "git"]
    assert all(e[1] not in {"push", "rebase", "commit"} for e in after)
    assert _LATCH.attempts(_KEY) == 0
    assert not scratch_path(client).exists()


def test_disarm_nonzero_with_unarmed_read_back_proceeds(
    monkeypatch: pytest.MonkeyPatch,
    log: list[LogEntry],
    git: ScriptedGit,
    client: ClientConfig,
    arm: FakeArmScript,
) -> None:
    FakeGhHead(log, armed=True, disarm=DISARM_NONZERO_UNARMED).install(monkeypatch)

    assert _dispatch(client) == TICKET

    assert "push" in _verbs(git)
    assert _failed() == []
    assert len(arm.calls()) == 1


def test_pre_push_head_move_is_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    log: list[LogEntry],
    git: ScriptedGit,
    client: ClientConfig,
    arm: FakeArmScript,
) -> None:
    FakeGhHead(log, reads={1: make_head(BRANCH, "d" * 40)}).install(monkeypatch)

    _dispatch(client)

    failed = _only_failure(D.PR_HEAD_MISMATCH)
    assert failed["evidence_snapshot"]["expected_sha"] == EXPECTED_SHA
    assert "push" not in _verbs(git)
    assert arm.calls() == []


def test_armed_pr_is_disarmed_right_before_the_push_and_rearmed_pinned(
    monkeypatch: pytest.MonkeyPatch,
    log: list[LogEntry],
    git: ScriptedGit,
    client: ClientConfig,
    arm: FakeArmScript,
) -> None:
    gh_head = FakeGhHead(log, armed=True).install(monkeypatch)

    assert _dispatch(client) == TICKET

    assert gh_head.disarm_calls == 1
    tail = [
        e for e in log if e[0] == "gh" or e[:2] in {("git", "push"), ("git", "diff")}
    ]
    disarm_at = tail.index(("gh", "disarm"))
    assert tail[disarm_at - 1] == ("gh", "fetch_pr_head")
    assert tail[disarm_at + 1] == ("gh", "fetch_pr_head")
    assert tail[disarm_at + 2][:2] == ("git", "push")
    assert all(e[:2] != ("git", "diff") for e in tail[disarm_at:])
    (argv,) = arm.calls()
    assert argv == [
        "arm-automerge",
        PR_NUMBER,
        "--repo-path",
        str(scratch_path(client)),
        "--head-sha",
        NEW_SHA,
    ]
    assert gh_head.armed is False
    (taken,) = _taken()
    assert taken["evidence_snapshot"]["auto_merge_was_armed"] is True


def test_armed_pr_failing_the_fence_ends_unarmed(
    monkeypatch: pytest.MonkeyPatch,
    log: list[LogEntry],
    git: ScriptedGit,
    client: ClientConfig,
    arm: FakeArmScript,
) -> None:
    gh_head = FakeGhHead(log, armed=True).install(monkeypatch)
    task = repair_task(computed_scope_tier="large", review_approver_kind="operator")

    assert _dispatch(client, task=task) == TICKET

    assert arm.calls() == []
    assert gh_head.armed is False
    (attention,) = _attention()
    assert attention["paused_status"] == "changelog_repair_unarmed"
    assert attention["auto_merge_was_armed"] is True
    (taken,) = _taken()
    assert taken["evidence_snapshot"]["expected_sha"] == EXPECTED_SHA
    assert taken["evidence_snapshot"]["new_sha"] == NEW_SHA
    assert _failed() == []


def test_third_party_arm_after_push_is_disarmed_when_fence_fails(
    monkeypatch: pytest.MonkeyPatch,
    log: list[LogEntry],
    git: ScriptedGit,
    client: ClientConfig,
    arm: FakeArmScript,
) -> None:
    gh_head = FakeGhHead(log, arm_after_push=True).install(monkeypatch)
    task = repair_task(computed_scope_tier="large")

    _dispatch(client, task=task)

    assert gh_head.disarm_calls == 1
    assert gh_head.armed is False
    push_at = next(i for i, e in enumerate(log) if e[:2] == ("git", "push"))
    assert log.index(("gh", "disarm")) > push_at
    assert len(_attention()) == 1


def test_unarmed_pr_is_never_disarmed(
    log: list[LogEntry],
    git: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
) -> None:
    assert _dispatch(client) == TICKET

    assert head.disarm_calls == 0
    (taken,) = _taken()
    assert taken["evidence_snapshot"]["auto_merge_was_armed"] is False


def test_post_push_disarm_failure_reports_armed(
    monkeypatch: pytest.MonkeyPatch,
    log: list[LogEntry],
    git: ScriptedGit,
    client: ClientConfig,
    arm: FakeArmScript,
) -> None:
    FakeGhHead(log, arm_after_push=True, disarm=DISARM_NONE).install(monkeypatch)
    task = repair_task(computed_scope_tier="large")

    _dispatch(client, task=task)

    assert len(_taken()) == 1
    (failed,) = _failed()
    assert failed["disposition"] == D.DISARM_FAILED.value
    assert "ARMED" in failed["error"]
    assert "PUSHED" in failed["error"]
    assert failed["evidence_snapshot"]["new_sha"] == NEW_SHA
    assert _attention() == []
    assert _LATCH.attempts(_KEY) == _MAX_ATTEMPTS


# --- dispatch: contracts on every call --------------------------------------


def test_every_git_call_is_bounded_and_mutations_carry_identity(
    git: ScriptedGit, head: FakeGhHead, client: ClientConfig, arm: FakeArmScript
) -> None:
    _dispatch(client)

    assert git.calls
    assert all(call.timeout is not None for call in git.calls)
    assert set(head.timeouts) == {git_mod._T_GH}
    for call in git.calls:
        if strip_flags(call.argv)[0] in {"rebase", "commit"}:
            assert call.argv[: len(_MUTATING_FLAGS)] == _MUTATING_FLAGS
    commits = [argv for argv in git.stripped() if argv[0] == "commit"]
    assert commits
    assert f"(#{TICKET})" in commits[0][-1]


def test_no_backup_ref_and_a_leased_push(
    git: ScriptedGit, head: FakeGhHead, client: ClientConfig, arm: FakeArmScript
) -> None:
    _dispatch(client)

    pushes = [argv for argv in git.stripped() if argv[0] == "push"]
    assert pushes == [
        (
            "push",
            "--no-verify",
            f"--force-with-lease=refs/heads/{BRANCH}:{EXPECTED_SHA}",
            "origin",
            f"HEAD:refs/heads/{BRANCH}",
        )
    ]


# --- dispatch: success ------------------------------------------------------


def test_success_orders_push_taken_fence_arm_then_cleanup(
    log: list[LogEntry],
    git: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="cw.reconcile.review_recipes")

    assert _dispatch(client) == TICKET

    push_at = next(i for i, e in enumerate(log) if e[:2] == ("git", "push"))
    taken_at = log.index(("event", str(OrchestratorEventType.PR_ACTION_TAKEN)))
    ls_remote_at = next(i for i, e in enumerate(log) if e[:2] == ("git", "ls-remote"))
    prunes = [i for i, e in enumerate(log) if e[1:3] == ("worktree", "prune")]
    removes = [i for i, e in enumerate(log) if e[1:3] == ("worktree", "remove")]
    assert push_at < taken_at < ls_remote_at
    assert prunes[-1] > ls_remote_at
    assert removes[-1] > prunes[-1]
    assert len(arm.calls()) == 1
    assert not scratch_path(client).exists()
    (taken,) = _taken()
    assert taken["evidence_snapshot"]["expected_sha"] == EXPECTED_SHA
    assert taken["evidence_snapshot"]["new_sha"] == NEW_SHA
    assert _failed() == []
    assert _LATCH.attempts(_KEY) == _MAX_ATTEMPTS
    line = next(
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("changelog_repair_succeeded")
    )
    assert f"expected_sha={EXPECTED_SHA}" in line
    assert f"new_sha={NEW_SHA}" in line
    assert "was_armed=False" in line
    assert "arm=armed" in line


def test_success_with_merged_arm_status(
    git: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    workspace: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    FakeArmScript(workspace, status="merged")
    caplog.set_level(logging.INFO, logger="cw.reconcile.review_recipes")

    assert _dispatch(client) == TICKET

    assert any("arm=merged" in r.getMessage() for r in caplog.records)


def test_arm_exit_3_is_a_logged_skip(
    git: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    workspace: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    FakeArmScript(workspace, exit_code=3, status="skipped")
    caplog.set_level(logging.INFO, logger="cw.reconcile.review_recipes")

    assert _dispatch(client) == TICKET

    assert _failed() == []
    assert any(
        r.getMessage().startswith("changelog_repair_arm_skipped")
        for r in caplog.records
    )


@pytest.mark.parametrize(
    ("disarm", "armed_note", "armed_after"),
    [
        (DISARM_OK, "auto-merge is verified unarmed", False),
        (DISARM_NONE, "auto-merge may still be ARMED", True),
    ],
)
def test_rearm_failure(
    git: ScriptedGit,
    log: list[LogEntry],
    client: ClientConfig,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    disarm: str,
    armed_note: str,
    armed_after: bool,
) -> None:
    FakeArmScript(workspace, exit_code=1, status="failed")
    gh_head = FakeGhHead(log, arm_after_push=True, disarm=disarm).install(monkeypatch)

    _dispatch(client)

    assert len(_taken()) == 1
    (failed,) = _failed()
    assert failed["disposition"] == D.REARM_FAILED.value
    assert "PR repaired but not re-armed" in failed["error"]
    assert armed_note in failed["error"]
    assert "arm script stderr" in failed["error"]
    push_at = next(i for i, e in enumerate(log) if e[:2] == ("git", "push"))
    assert log.index(("gh", "disarm")) > push_at
    assert gh_head.disarm_calls == 1
    assert gh_head.armed is armed_after
    assert not scratch_path(client).exists()


def test_second_tick_after_success_fires_nothing(
    act_clients: dict[str, ClientConfig],
    git: ScriptedGit,
    head: FakeGhHead,
    arm: FakeArmScript,
) -> None:
    save_dev_queue(DevQueueStore(tasks=[repair_task()]))
    (job,) = _act(act_clients)
    _dispatch_changelog_repair(job)

    assert _act(act_clients) == []
    assert _failed() == []


# --- dispatch: fence outcomes -----------------------------------------------


@pytest.mark.parametrize(
    "task",
    [
        pytest.param(
            repair_task(computed_scope_tier="large", review_approved_sha="nothex"),
            id="large-stale",
        ),
        pytest.param(repair_task(computed_scope_tier="small"), id="clean-no-ledger"),
    ],
)
def test_fence_failure_leaves_the_pr_unarmed_with_an_attention_page(
    git: ScriptedGit,
    head: FakeGhHead,
    workspace: Path,
    arm: FakeArmScript,
    caplog: pytest.LogCaptureFixture,
    task: Any,
) -> None:
    clean = task.computed_scope_tier == "small"
    client = make_client(workspace, review={"convergence": "clean"} if clean else {})
    caplog.set_level(logging.INFO, logger="cw.reconcile.review_recipes")

    assert _dispatch(client, task=task) == TICKET

    assert arm.calls() == []
    assert _failed() == []
    (attention,) = _attention()
    assert attention["paused_status"] == "changelog_repair_unarmed"
    assert attention["review_tip_check"] == "stale"
    assert attention["disposition"] == "unarmed_review_fence"
    assert attention["expected_sha"] == EXPECTED_SHA
    assert attention["new_sha"] == NEW_SHA
    assert attention["recipe"] == RECIPE_REPAIR_CHANGELOG_CONFLICT
    crumbs = attention["breadcrumbs"]
    release = crumbs.split("Release: ", 1)[1].split(" (", 1)[0]
    assert release == f"cw dev-queue requeue {TICKET} -c acme --from-completed"
    assert any(
        r.getMessage().startswith("changelog_repair_pushed_unarmed")
        for r in caplog.records
    )


def test_small_non_clean_row_arms(
    git: ScriptedGit, head: FakeGhHead, client: ClientConfig, arm: FakeArmScript
) -> None:
    assert _dispatch(client) == TICKET

    assert len(arm.calls()) == 1
    assert _attention() == []


# --- dispatch: crash after the push -----------------------------------------


def test_crash_after_push_is_post_push_failed_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    log: list[LogEntry],
    git: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
) -> None:
    def _boom(*_a: object, **_kw: object) -> object:
        msg = "fence bug"
        raise RuntimeError(msg)

    monkeypatch.setattr(repair, "evaluate_repair_fence", _boom)

    assert _dispatch(client) is None

    assert len(_taken()) == 1
    (failed,) = _failed()
    assert failed["disposition"] == D.POST_PUSH_FAILED.value
    assert failed["evidence_snapshot"]["expected_sha"] == EXPECTED_SHA
    assert failed["evidence_snapshot"]["new_sha"] == NEW_SHA
    assert "fence bug" in failed["error"]
    failed_at = log.index(("event", str(OrchestratorEventType.PR_ACTION_FAILED)))
    removes = [i for i, e in enumerate(log) if e[1:3] == ("worktree", "remove")]
    assert removes[-1] < failed_at
    reads_after_push = [
        e
        for e in log[next(i for i, e in enumerate(log) if e[:2] == ("git", "push")) :]
        if e == ("gh", "fetch_pr_head")
    ]
    assert reads_after_push
    assert _LATCH.attempts(_KEY) == _MAX_ATTEMPTS
    assert not scratch_path(client).exists()


def test_rewrite_stop_is_reported_after_cleanup(
    log: list[LogEntry],
    git: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
) -> None:
    git.set(("push",), GitResult(1))

    _dispatch(client)

    failed_at = log.index(("event", str(OrchestratorEventType.PR_ACTION_FAILED)))
    removes = [i for i, e in enumerate(log) if e[1:3] == ("worktree", "remove")]
    assert removes[-1] < failed_at
    assert not scratch_path(client).exists()


def test_cleanup_failure_is_logged_not_raised(
    git: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    caplog: pytest.LogCaptureFixture,
) -> None:
    git.set(("worktree", "prune"), TIMEOUT)

    assert _dispatch(client) == TICKET

    assert any("changelog_repair_cleanup" in r.getMessage() for r in caplog.records)


# --- pins -------------------------------------------------------------------


def test_recipe_is_not_in_the_fired_at_maps() -> None:
    assert RECIPE_REPAIR_CHANGELOG_CONFLICT not in RECIPE_ATTENTION_STATES
    assert RECIPE_REPAIR_CHANGELOG_CONFLICT not in RECIPE_FIRED_AT_GETTERS
    assert set(RECIPE_FIRED_AT_GETTERS) == set(RECIPE_ATTENTION_STATES)


def test_validator_accepts_the_new_key() -> None:
    lane = LaneConfig(
        name="default", review_recipes={RECIPE_REPAIR_CHANGELOG_CONFLICT: True}
    )

    assert lane.review_recipes == {RECIPE_REPAIR_CHANGELOG_CONFLICT: True}


@pytest.mark.parametrize("doc", ["docs/events.md", "docs/dispatch-runbook.md"])
def test_every_disposition_is_documented(doc: str) -> None:
    text = (_REPO_ROOT / doc).read_text(encoding="utf-8")

    missing = [d.value for d in D if f"`{d.value}`" not in text]

    assert missing == []


def test_unarmed_paused_status_and_release_are_documented() -> None:
    events = (_REPO_ROOT / "docs/events.md").read_text(encoding="utf-8")
    runbook = (_REPO_ROOT / "docs/dispatch-runbook.md").read_text(encoding="utf-8")

    assert "`changelog_repair_unarmed`" in events
    assert "--from-completed" in runbook


def test_documented_release_is_legal_and_stage_review_is_refused(
    tmp_config_dir: Path, workspace: Path
) -> None:
    write_clients_yaml(ClientSpec("acme", workspace, default_branch="main"))
    task = repair_task(stage=Stage.FINALIZE, session_id=None)
    save_dev_queue(DevQueueStore(tasks=[task]))
    daemon = FakeNativeDaemonClient()

    precheck_requeue(TICKET, "acme", from_completed=True, native_daemon=daemon)

    with pytest.raises(RequeueStageError):
        precheck_requeue(
            TICKET, "acme", "review", from_completed=True, native_daemon=daemon
        )


def test_latch_helpers() -> None:
    key = ("c", "t", "u")
    _LATCH.bump(key)
    assert _LATCH.attempts(key) == 1
    assert not _LATCH.blocked(key)
    _LATCH.saturate(key)
    assert _LATCH.blocked(key)
    _LATCH.sweep(set())
    assert _LATCH.attempts(key) == 0


def test_main_text_is_written_through_unchanged_when_nothing_to_insert(
    git: ScriptedGit, head: FakeGhHead, client: ClientConfig, arm: FakeArmScript
) -> None:
    git.set(("show", f"{EXPECTED_SHA}:CHANGELOG.md"), GitResult(stdout=CL_BASE))
    scratch = scratch_path(client)

    def _add(argv: tuple[str, ...]) -> GitResult:
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "CHANGELOG.md").write_text(CL_MAIN, encoding="utf-8")
        return GitResult()

    git.set(("worktree", "add"), _add)

    assert _dispatch(client) == TICKET

    assert "commit" not in _verbs(git)


# --- dispatch: remaining git/script error branches ----------------------------


@pytest.mark.usefixtures("head", "arm")
@pytest.mark.parametrize(
    ("prefix", "response", "disposition"),
    [
        (("fetch", "origin"), OSERROR, D.FETCH_FAILED),
        (("rev-parse", "--verify", "--quiet"), GitResult(1), D.FETCH_FAILED),
        (("rev-list", "--count"), GitResult(stdout="many\n"), D.REBASE_FAILED),
        (("commit",), GitResult(1, "", "no identity"), D.REBASE_FAILED),
        (("rev-parse", "HEAD"), GitResult(128), D.REBASE_FAILED),
    ],
)
def test_git_step_failures_park(
    git: ScriptedGit,
    client: ClientConfig,
    prefix: tuple[str, ...],
    response: Any,
    disposition: D,
) -> None:
    git.set(prefix, response)

    assert _dispatch(client) is None

    _only_failure(disposition)
    assert "push" not in _verbs(git)


@pytest.mark.usefixtures("head", "arm")
@pytest.mark.parametrize(
    ("prefix", "response"),
    [
        (("worktree", "prune"), GitResult(1, "", "locked")),
        (("ls-remote",), TIMEOUT),
        (("ls-remote",), GitResult(128)),
    ],
)
def test_best_effort_steps_do_not_fail_the_repair(
    git: ScriptedGit,
    client: ClientConfig,
    prefix: tuple[str, ...],
    response: Any,
) -> None:
    git.set(prefix, response)

    assert _dispatch(client) == TICKET

    assert _failed() == []


@pytest.mark.usefixtures("head", "arm")
def test_scratch_remove_failure_is_logged(
    git: ScriptedGit, client: ClientConfig, caplog: pytest.LogCaptureFixture
) -> None:
    git.set(("worktree", "remove"), GitResult(1, "", "busy"))

    assert _dispatch(client) == TICKET

    assert any("step=remove" in r.getMessage() for r in caplog.records)


@pytest.mark.usefixtures("head", "arm")
def test_failed_abort_still_parks(git: ScriptedGit, client: ClientConfig) -> None:
    git.set(("rebase", "--abort"), TIMEOUT)
    git.set(("rebase", DEFAULT_REF), GitResult(1))

    _dispatch(client)

    _only_failure(D.REBASE_FAILED)


@pytest.mark.usefixtures("head", "arm")
def test_unavailable_range_diff_is_noted(
    git: ScriptedGit, client: ClientConfig
) -> None:
    outputs = iter(["+x = 1\n", "+x = 2\n"])
    git.set(("diff", "-U0"), lambda _argv: GitResult(stdout=next(outputs)))
    git.set(("range-diff",), TIMEOUT)

    _dispatch(client)

    failed = _only_failure(D.VERIFY_FAILED)
    assert "unavailable" in failed["error"]


@pytest.mark.usefixtures("head", "arm")
def test_gate_timeout_fails(
    git: ScriptedGit, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_mod, "_T_GATE", 0.2)
    _plant_gate(client, "import time\ntime.sleep(5)\n")

    _dispatch(client)

    failed = _only_failure(D.GATE_FAILED)
    assert "did not run" in failed["error"]


@pytest.mark.usefixtures("head")
def test_arm_timeout_is_rearm_failed(
    git: ScriptedGit,
    client: ClientConfig,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(git_mod, "_T_ARM", 0.2)
    FakeArmScript(workspace, sleep=5)

    _dispatch(client)

    (failed,) = _failed()
    assert failed["disposition"] == D.REARM_FAILED.value
    assert "exit None" in failed["error"]


@pytest.mark.usefixtures("git", "head")
def test_arm_with_unparseable_stdout_still_counts_as_armed(
    client: ClientConfig, workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    FakeArmScript(workspace, stdout="not json")
    caplog.set_level(logging.INFO, logger="cw.reconcile.review_recipes")

    assert _dispatch(client) == TICKET

    assert any("arm=armed" in r.getMessage() for r in caplog.records)


@pytest.mark.usefixtures("git", "arm")
def test_unreadable_head_after_the_push_is_disarm_failed(
    monkeypatch: pytest.MonkeyPatch, log: list[LogEntry], client: ClientConfig
) -> None:
    FakeGhHead(log, reads={2: None}).install(monkeypatch)
    task = repair_task(computed_scope_tier="large")

    _dispatch(client, task=task)

    (failed,) = _failed()
    assert failed["disposition"] == D.DISARM_FAILED.value
    assert "armed state unknown" in failed["error"]


@pytest.mark.usefixtures("arm")
def test_push_rejected_after_a_disarm_says_not_rearmed(
    monkeypatch: pytest.MonkeyPatch,
    log: list[LogEntry],
    git: ScriptedGit,
    client: ClientConfig,
) -> None:
    FakeGhHead(log, armed=True).install(monkeypatch)
    git.set(("push",), GitResult(1))

    _dispatch(client)

    failed = _only_failure(D.PUSH_REJECTED)
    assert "disarmed and is NOT re-armed" in failed["error"]
    assert failed["evidence_snapshot"]["auto_merge_was_armed"] is True


# --- dispatch: pre-push contracts (fix cycle 1) -----------------------------


@pytest.mark.usefixtures("git", "arm")
def test_pr_head_mismatch_names_the_observed_base_and_state(
    monkeypatch: pytest.MonkeyPatch, log: list[LogEntry], client: ClientConfig
) -> None:
    FakeGhHead(log, base_ref_name="develop", state="CLOSED").install(monkeypatch)

    _dispatch(client)

    failed = _only_failure(D.PR_HEAD_MISMATCH)
    assert "state=CLOSED" in failed["error"]
    assert "base=develop" in failed["error"]


@pytest.mark.usefixtures("git", "arm")
def test_pr_closed_or_rebased_onto_another_base_before_the_push_parks(
    monkeypatch: pytest.MonkeyPatch, log: list[LogEntry], client: ClientConfig
) -> None:
    FakeGhHead(log, reads={1: make_head(state="MERGED")}).install(monkeypatch)

    _dispatch(client)

    _only_failure(D.PR_HEAD_MISMATCH)
    assert ("git", "push") not in [e[:2] for e in log]


@pytest.mark.usefixtures("head", "arm")
def test_pre_image_is_logged_before_the_push(
    git: ScriptedGit, client: ClientConfig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="cw.reconcile.review_recipes")
    seen: list[list[str]] = []

    def _push(_argv: tuple[str, ...]) -> GitResult:
        seen.append(
            [
                r.getMessage()
                for r in caplog.records
                if r.getMessage().startswith("changelog_repair_pushing")
            ]
        )
        return GitResult()

    git.set(("push",), _push)

    assert _dispatch(client) == TICKET

    ((line,),) = seen
    for expected in ("client=acme", f"ticket={TICKET}", EXPECTED_SHA, NEW_SHA, BRANCH):
        assert expected in line


@pytest.mark.usefixtures("head", "arm")
def test_skipped_branch_bullets_are_logged_and_counted(
    git: ScriptedGit, client: ClientConfig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="cw.reconcile.review_recipes")
    released_with_gamma = CL_MAIN.replace(
        "- Beta fix (#2).\n\n", "- Beta fix (#2).\n\n- Gamma fix (#3).\n\n", 1
    )
    git.set(
        ("show", f"{DEFAULT_REF}:CHANGELOG.md"), GitResult(stdout=released_with_gamma)
    )

    assert _dispatch(client) == TICKET

    messages = [r.getMessage() for r in caplog.records]
    skipped = [m for m in messages if m.startswith("changelog_repair_bullet_skipped")]
    assert len(skipped) == 1
    assert "Gamma fix (#3)." in skipped[0]
    (success,) = [m for m in messages if m.startswith("changelog_repair_succeeded")]
    assert "bullets_skipped=1" in success
    assert "bullets_inserted=0" in success


@pytest.mark.usefixtures("head", "arm")
def test_changelog_write_oserror_is_parked_with_a_disposition(
    git: ScriptedGit, client: ClientConfig
) -> None:
    scratch = scratch_path(client)

    def _add(_argv: tuple[str, ...]) -> GitResult:
        (scratch / "CHANGELOG.md").mkdir(parents=True, exist_ok=True)
        return GitResult()

    git.set(("worktree", "add"), _add)

    assert _dispatch(client) is None

    failed = _only_failure(D.REBASE_FAILED)
    assert "could not write CHANGELOG.md" in failed["error"]
    assert failed["evidence_snapshot"]["expected_sha"] == EXPECTED_SHA
    assert _LATCH.blocked(_KEY)
    assert "push" not in _verbs(git)
    assert not scratch.exists()


@pytest.mark.usefixtures("head", "arm", "git")
@pytest.mark.parametrize("error", [OSError("disk gone"), CwError("cw broke")])
def test_unexpected_pre_push_error_is_parked_not_escaped(
    monkeypatch: pytest.MonkeyPatch, client: ClientConfig, error: Exception
) -> None:
    def _boom(*_a: object, **_kw: object) -> None:
        raise error

    monkeypatch.setattr(repair, "_stage_rewrite", _boom)

    assert _dispatch(client) is None

    failed = _only_failure(D.REBASE_FAILED)
    assert type(error).__name__ in failed["error"]
    assert failed["evidence_snapshot"]["expected_sha"] == EXPECTED_SHA
    assert failed["evidence_snapshot"]["auto_merge_was_armed"] is False
    assert _LATCH.blocked(_KEY)


@pytest.mark.usefixtures("head", "arm")
def test_failed_git_add_is_reported_as_the_cause(
    git: ScriptedGit, client: ClientConfig
) -> None:
    git.set(("add",), GitResult(128, "", "index.lock exists"))

    assert _dispatch(client) is None

    failed = _only_failure(D.REBASE_FAILED)
    assert "git add exit 128" in failed["error"]
    assert "index.lock" in failed["error"]
    assert "commit" not in _verbs(git)


@pytest.mark.usefixtures("head", "arm")
def test_failed_git_add_during_a_rebase_stop_aborts_the_rebase(
    git: ScriptedGit, client: ClientConfig
) -> None:
    git.set(("rebase", DEFAULT_REF), GitResult(1))
    git.set(
        ("diff", "--name-only", "--diff-filter=U"), GitResult(stdout="CHANGELOG.md\n")
    )
    git.set(("add",), GitResult(128, "", "index.lock exists"))

    _dispatch(client)

    failed = _only_failure(D.REBASE_FAILED)
    assert "git add exit 128" in failed["error"]
    assert ("rebase", "--abort") in git.stripped()


@pytest.mark.usefixtures("head", "arm")
def test_crlf_blobs_reach_the_rebuild_unchanged(
    git: ScriptedGit, client: ClientConfig
) -> None:
    """``git show`` is read without newline translation, so mixed endings park."""
    git.set(
        ("show", f"{DEFAULT_REF}:CHANGELOG.md"),
        GitResult(stdout=CL_MAIN.replace("\n", "\r\n", 3)),
    )

    _dispatch(client)

    _only_failure(D.CHANGELOG_UNPARSEABLE)
    assert all(call.argv[0] != "show" or "text" not in call.argv for call in git.calls)
    assert "worktree" not in _verbs(git)


# --- wiring: the real act-built job through the post-lock sink --------------


@pytest.mark.usefixtures("wired", "git", "head")
def test_act_built_job_runs_through_the_post_lock_sink(workspace: Path) -> None:
    FakeArmScript(workspace)
    config = _config()
    slugs = capture_review_repo_slugs(config=config, dispatching=True)
    jobs = DeferredReconcileJobs(review=DeferredReviewDispatch())

    run_review_recipes(config=config, jobs=jobs, repo_slugs=slugs)
    run_post_lock_jobs(jobs)

    assert len(_taken()) == 1
    assert _failed() == []
    second = DeferredReconcileJobs(review=DeferredReviewDispatch())
    run_review_recipes(config=config, jobs=second, repo_slugs=slugs)
    assert second.post_lock == []
