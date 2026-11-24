"""``dispatch_repair`` tests for the clean-mode ledger carry (#2921).

Hermetic: ``ScriptedGit`` stands in for ``run_git``, ``FakeGhHead`` for the gh
head read and disarm, ``FakeArmScript`` for the arm subprocess, and the ledger is
a real file under a tmp worktree. Tests assert outcomes (ledger bytes, arm
called, events), not git wording. Real-git proof coverage lives in
``tests/test_repair_proof.py`` and ``tests/test_changelog_repair_real_git.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun import freeze_time

from cw.models import ClientConfig
from cw.reconcile.review_recipes import RECIPE_REPAIR_CHANGELOG_CONFLICT, repair_ledger
from cw.reconcile.review_recipes import repair_engine as engine
from cw.reconcile.review_recipes.repair_engine import dispatch_repair
from cw.reconcile.review_recipes.repair_fence import FenceVerdict
from cw.repair_disposition import RepairDisposition as D
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    CleanShaRemap,
    ConvergenceLedger,
    evaluate_tip,
    load_ledger,
)
from tests._changelog_cut_helpers import CL_NEW_MAIN_LINE_DELETED, TICKET
from tests._changelog_repair_helpers import (
    DEFAULT_REF,
    DEFAULT_SHA,
    NEW_SHA,
    OSERROR,
    PR_NUMBER,
    REBASED_SHA,
    TIMEOUT,
    FakeArmScript,
    FakeGhHead,
    GitResult,
    LogEntry,
    ScriptedGit,
    clean_client,
    make_client,
    make_repair_job,
    proof_tree_calls,
    repair_task,
    scratch_path,
    stub_remap_proof,
)
from tests._changelog_repair_helpers import EXPECTED_SHA as OLD_SHA
from tests._changelog_repair_helpers import attention as _attention
from tests._changelog_repair_helpers import failed as _failed
from tests._changelog_repair_helpers import taken as _taken
from tests._review_convergence_helpers import (
    REMAP_NEW_SHA,
    REMAP_OLD_SHA,
    TICKET_ID,
    clean_sha_remap,
    confirmed_ledger,
    write_ledger,
)

pytestmark = pytest.mark.usefixtures("repair_events")

FROZEN_STAMP = "2026-11-07T12:00:00Z"
COMMITS_CARRIED = 1
PROOF_MERGE_TREE = ("merge-tree", "--write-tree", "--no-messages")
OTHER_TICKET = "OTHER-9"


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def git(monkeypatch: pytest.MonkeyPatch, log: list[LogEntry]) -> ScriptedGit:
    return ScriptedGit(log).install(monkeypatch)


@pytest.fixture
def client(repair_workspace: Path) -> ClientConfig:
    return clean_client(repair_workspace)


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    return tmp_path / "row-worktree"


@pytest.fixture
def ledger_path(worktree: Path) -> Path:
    """A ledger converged at the pre-repair head, owned by the repaired ticket."""
    return write_ledger(worktree, confirmed_ledger(sha=OLD_SHA, ticket_id=TICKET))


@pytest.fixture
def proven(git: ScriptedGit) -> ScriptedGit:
    """*git* answering a provable rebase: tree-equal plus a CHANGELOG-only commit."""
    stub_remap_proof(git)
    return git


def _dispatch(client: ClientConfig, worktree: Path | None, **task: Any) -> str | None:
    row = repair_task(worktree_path=worktree, **task)
    return dispatch_repair(make_repair_job(client, task=row))


def _assert_unarmed_page(
    git: ScriptedGit, head: FakeGhHead, arm: FakeArmScript
) -> dict[str, Any]:
    """The PR was pushed, never armed, and one fence page said why."""
    assert any(argv[0] == "push" for argv in git.stripped())
    assert arm.calls() == []
    assert head.armed is False
    assert len(_taken()) == 1
    assert _failed() == []
    (page,) = _attention()
    assert page["disposition"] == "unarmed_review_fence"
    assert page["review_tip_check"] == "stale"
    return page


def _assert_unarmed_and_untouched(
    git: ScriptedGit,
    head: FakeGhHead,
    arm: FakeArmScript,
    path: Path,
    before: bytes,
) -> None:
    _assert_unarmed_page(git, head, arm)
    assert path.read_bytes() == before


# --- happy path and ordering --------------------------------------------------


@freeze_time(FROZEN_STAMP)
def test_proven_rebase_remaps_the_ledger_and_arms_the_new_head(
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
) -> None:
    assert _dispatch(client, worktree) == TICKET

    (argv,) = arm.calls()
    assert argv[-2:] == ["--head-sha", NEW_SHA]
    assert argv[:2] == ["arm-automerge", PR_NUMBER]
    assert _attention() == []
    assert len(_taken()) == 1
    assert _failed() == []
    ledger = load_ledger(ledger_path)
    assert ledger is not None
    assert ledger.last_clean_sha == NEW_SHA
    assert ledger.clean_sha_remaps == [
        CleanShaRemap(
            at=FROZEN_STAMP,
            recipe=RECIPE_REPAIR_CHANGELOG_CONFLICT,
            from_sha=OLD_SHA,
            rebased_sha=REBASED_SHA,
            to_sha=NEW_SHA,
            commits=COMMITS_CARRIED,
        )
    ]


def test_push_precedes_the_ledger_save_which_precedes_the_arm(
    monkeypatch: pytest.MonkeyPatch,
    log: list[LogEntry],
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
) -> None:
    real_save = repair_ledger.save_ledger
    real_arm = engine._run_arm

    def logged_save(path: Path, ledger: ConvergenceLedger) -> None:
        log.append(("ledger", "save"))
        real_save(path, ledger)

    def logged_arm(*args: Any, **kwargs: Any) -> Any:
        log.append(("arm",))
        return real_arm(*args, **kwargs)

    monkeypatch.setattr(repair_ledger, "save_ledger", logged_save)
    monkeypatch.setattr(engine, "_run_arm", logged_arm)

    assert _dispatch(client, worktree) == TICKET

    push_at = next(i for i, e in enumerate(log) if e[:2] == ("git", "push"))
    save_at = log.index(("ledger", "save"))
    arm_at = log.index(("arm",))
    assert push_at < save_at < arm_at


# --- the proof fails: the PR is pushed and left unarmed -----------------------


def test_stray_tree_difference_leaves_the_pr_unarmed(
    git: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
) -> None:
    stub_remap_proof(git, tree_diff="src/x.py\0CHANGELOG.md\0")
    before = ledger_path.read_bytes()

    assert _dispatch(client, worktree) == TICKET

    _assert_unarmed_and_untouched(git, head, arm, ledger_path, before)


def test_unconfined_changelog_leaves_the_pr_unarmed(
    git: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
) -> None:
    stub_remap_proof(git, new_text=CL_NEW_MAIN_LINE_DELETED)
    before = ledger_path.read_bytes()

    assert _dispatch(client, worktree) == TICKET

    _assert_unarmed_and_untouched(git, head, arm, ledger_path, before)


def test_unarmed_page_names_why_the_remap_was_skipped(
    git: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
) -> None:
    stub_remap_proof(git, tree_diff="src/x.py\0")

    assert _dispatch(client, worktree) == TICKET

    (page,) = _attention()
    assert "ledger remap skipped:" in str(page)
    assert "src/x.py" in str(page)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(GitResult(returncode=128, stderr="fatal"), id="nonzero-exit"),
        pytest.param(GitResult(stdout="garbage"), id="unreadable"),
        pytest.param(TIMEOUT, id="timeout"),
        pytest.param(OSERROR, id="oserror"),
    ],
)
def test_unavailable_merge_tree_leaves_unarmed(
    git: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
    answer: Any,
) -> None:
    stub_remap_proof(git)
    git.set(PROOF_MERGE_TREE, answer)
    before = ledger_path.read_bytes()

    assert _dispatch(client, worktree) == TICKET

    _assert_unarmed_and_untouched(git, head, arm, ledger_path, before)


@pytest.mark.parametrize(
    ("changed", "parent"),
    [
        pytest.param("src/x.py\0CHANGELOG.md\0", REBASED_SHA, id="stray-path"),
        pytest.param("CHANGELOG.md\0", "f" * 40, id="parent-mismatch"),
    ],
)
def test_non_changelog_rebuild_delta_leaves_unarmed(
    git: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
    changed: str,
    parent: str,
) -> None:
    stub_remap_proof(git, changed=changed, parent=parent)
    before = ledger_path.read_bytes()

    assert _dispatch(client, worktree) == TICKET

    _assert_unarmed_and_untouched(git, head, arm, ledger_path, before)


# --- the ledger is not carried -------------------------------------------------


def test_missing_ledger_stays_stale_and_is_not_created(
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
) -> None:
    assert _dispatch(client, worktree) == TICKET

    _assert_unarmed_page(proven, head, arm)
    assert not (worktree / LEDGER_RELATIVE_PATH).exists()


def test_row_without_a_worktree_stays_stale_with_no_proof(
    git: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
) -> None:
    assert _dispatch(client, None) == TICKET

    _assert_unarmed_page(git, head, arm)
    assert proof_tree_calls(git) == []


def test_foreign_ticket_ledger_stays_stale(
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
) -> None:
    path = write_ledger(worktree, confirmed_ledger(sha=OLD_SHA, ticket_id=OTHER_TICKET))
    before = path.read_bytes()

    assert _dispatch(client, worktree) == TICKET

    _assert_unarmed_and_untouched(proven, head, arm, path, before)


@pytest.mark.parametrize("status", ["in_progress", "stopped", "awaiting_confirm"])
def test_unconverged_ledger_is_not_remapped(
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    status: str,
) -> None:
    ledger = confirmed_ledger(sha=OLD_SHA, ticket_id=TICKET)
    path = write_ledger(worktree, ledger.model_copy(update={"status": status}))
    before = path.read_bytes()

    assert _dispatch(client, worktree) == TICKET

    _assert_unarmed_and_untouched(proven, head, arm, path, before)


def test_clean_sha_not_the_old_head_is_not_remapped(
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
) -> None:
    path = write_ledger(worktree, confirmed_ledger(sha="e" * 40, ticket_id=TICKET))
    before = path.read_bytes()

    assert _dispatch(client, worktree) == TICKET

    _assert_unarmed_and_untouched(proven, head, arm, path, before)


def test_unreadable_origin_tip_is_not_remapped(
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
) -> None:
    proven.set(("ls-remote",), GitResult())
    before = ledger_path.read_bytes()

    assert _dispatch(client, worktree) == TICKET

    _assert_unarmed_and_untouched(proven, head, arm, ledger_path, before)


def test_origin_tip_mismatch_is_not_remapped(
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
) -> None:
    proven.set(("ls-remote",), GitResult(stdout=f"{'9' * 40}\trefs/heads/x\n"))
    before = ledger_path.read_bytes()

    assert _dispatch(client, worktree) == TICKET

    _assert_unarmed_and_untouched(proven, head, arm, ledger_path, before)


def test_failed_approval_does_not_persist_the_remap(
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
) -> None:
    before = ledger_path.read_bytes()

    assert (
        _dispatch(
            client,
            worktree,
            computed_scope_tier="large",
            review_approver_kind="operator",
            review_approved_sha="abc1234",
        )
        == TICKET
    )

    page = _assert_unarmed_page(proven, head, arm)
    assert page["review_approval_check"] == "stale"
    assert ledger_path.read_bytes() == before


def test_concurrent_ledger_change_is_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
) -> None:
    real_fence = engine.evaluate_repair_fence
    intruder = confirmed_ledger(sha="c" * 40, ticket_id=TICKET)

    def fence_then_intrusion(*args: Any, **kwargs: Any) -> Any:
        verdict = real_fence(*args, **kwargs)
        write_ledger(worktree, intruder)
        return verdict

    monkeypatch.setattr(engine, "evaluate_repair_fence", fence_then_intrusion)

    assert _dispatch(client, worktree) == TICKET

    _assert_unarmed_page(proven, head, arm)
    assert load_ledger(ledger_path) == intruder


def test_persist_failure_leaves_unarmed(
    monkeypatch: pytest.MonkeyPatch,
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
) -> None:
    def disk_full(_path: Path, _ledger: ConvergenceLedger) -> None:
        message = "disk full"
        raise OSError(message)

    monkeypatch.setattr(repair_ledger, "save_ledger", disk_full)
    before = ledger_path.read_bytes()

    assert _dispatch(client, worktree) == TICKET

    _assert_unarmed_and_untouched(proven, head, arm, ledger_path, before)


@pytest.mark.parametrize(
    ("push", "disposition"),
    [
        pytest.param(GitResult(1, "", "stale info"), D.PUSH_REJECTED, id="rejected"),
        pytest.param(TIMEOUT, D.PUSH_OUTCOME_UNKNOWN, id="outcome-unknown"),
    ],
)
def test_push_failure_never_touches_the_ledger(
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
    push: Any,
    disposition: D,
) -> None:
    proven.set(("push",), push)
    before = ledger_path.read_bytes()

    assert _dispatch(client, worktree) is None

    (failed,) = _failed()
    assert failed["disposition"] == disposition.value
    assert _taken() == []
    assert _attention() == []
    assert arm.calls() == []
    assert ledger_path.read_bytes() == before


# --- the carry survives a failed re-arm ----------------------------------------


def test_rearm_failure_after_the_persist_keeps_the_remap(
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    repair_workspace: Path,
    worktree: Path,
    ledger_path: Path,
) -> None:
    FakeArmScript(repair_workspace, exit_code=1, status="failed")

    assert _dispatch(client, worktree) is None

    (failed,) = _failed()
    assert failed["disposition"] == D.REARM_FAILED.value
    ledger = load_ledger(ledger_path)
    assert ledger is not None
    assert ledger.last_clean_sha == NEW_SHA
    assert len(ledger.clean_sha_remaps) == 1
    verdict = evaluate_tip(ledger, head_sha=NEW_SHA, origin_tip_sha=NEW_SHA)
    assert verdict.decision == "accepted"
    assert not scratch_path(client).exists()


# --- rows that must stay byte-identical ----------------------------------------


def test_non_clean_client_runs_no_proof(
    git: ScriptedGit,
    head: FakeGhHead,
    arm: FakeArmScript,
    repair_workspace: Path,
    worktree: Path,
    ledger_path: Path,
) -> None:
    client = make_client(repair_workspace)
    before = ledger_path.read_bytes()

    assert _dispatch(client, worktree) == TICKET

    assert proof_tree_calls(git) == []
    assert len(arm.calls()) == 1
    assert _attention() == []
    assert ledger_path.read_bytes() == before


def test_strict_opt_out_runs_no_proof_and_stays_unarmed(
    git: ScriptedGit,
    head: FakeGhHead,
    arm: FakeArmScript,
    repair_workspace: Path,
    worktree: Path,
    ledger_path: Path,
) -> None:
    client = clean_client(repair_workspace, strict=True)
    before = ledger_path.read_bytes()

    assert _dispatch(client, worktree) == TICKET

    _assert_unarmed_and_untouched(git, head, arm, ledger_path, before)
    assert proof_tree_calls(git) == []


# --- the one pinned default sha (#3375) ----------------------------------------


def test_rebase_blobs_and_proof_use_the_one_pinned_sha(
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
) -> None:
    assert _dispatch(client, worktree) == TICKET

    calls = proven.stripped()
    (rebase,) = [
        argv for argv in calls if argv[0] == "rebase" and "--continue" not in argv
    ]
    assert rebase[1] == DEFAULT_SHA
    shows = [argv[1] for argv in calls if argv[0] == "show"]
    assert f"{DEFAULT_SHA}:CHANGELOG.md" in shows
    assert f"{DEFAULT_REF}:CHANGELOG.md" not in shows
    (proof_tree,) = proof_tree_calls(proven)
    assert proof_tree[-2] == DEFAULT_SHA
    counted = [argv for argv in calls if argv[:2] == ("rev-list", "--count")]
    assert f"{DEFAULT_SHA}..{REBASED_SHA}" in [argv[2] for argv in counted]
    named = [argv for argv in calls if DEFAULT_REF in " ".join(argv)]
    assert {argv[0] for argv in named} <= {
        "diff",
        "merge-base",
        "merge-tree",
        "rev-parse",
    }  # the pin and _conflict_set only


def test_default_ref_that_does_not_resolve_stops_before_any_rewrite(
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
) -> None:
    proven.set(
        ("rev-parse", "--verify", "--quiet", f"{DEFAULT_REF}^{{commit}}"), GitResult(1)
    )

    assert _dispatch(client, worktree) is None

    (failed,) = _failed()
    assert failed["disposition"] == D.FETCH_FAILED.value
    verbs = [argv[0] for argv in proven.stripped()]
    assert "push" not in verbs
    assert "rebase" not in verbs
    assert arm.calls() == []


def test_merge_base_that_drifts_after_prepare_stops_before_any_rewrite(
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
) -> None:
    proven.set(("merge-base", DEFAULT_SHA), GitResult(stdout=f"{'9' * 40}\n"))

    assert _dispatch(client, worktree) is None

    (failed,) = _failed()
    assert failed["disposition"] == D.CONFLICT_UNEXPLAINED.value
    verbs = [argv[0] for argv in proven.stripped()]
    assert "push" not in verbs
    assert "rebase" not in verbs


def test_merge_base_that_cannot_be_derived_stops_before_any_rewrite(
    proven: ScriptedGit,
    head: FakeGhHead,
    client: ClientConfig,
    arm: FakeArmScript,
    worktree: Path,
    ledger_path: Path,
) -> None:
    proven.set(("merge-base", DEFAULT_SHA), GitResult(1, "", "fatal"))

    assert _dispatch(client, worktree) is None

    (failed,) = _failed()
    assert failed["disposition"] == D.CONFLICT_UNEXPLAINED.value
    assert "rebase" not in [argv[0] for argv in proven.stripped()]


# --- persist_remap -----------------------------------------------------------


def test_persist_writes_the_remap_when_the_pre_image_matches(tmp_path: Path) -> None:
    before = confirmed_ledger(sha=REMAP_OLD_SHA)
    path = write_ledger(tmp_path, before)
    after = before.model_copy(update={"last_clean_sha": REMAP_NEW_SHA})

    assert repair_ledger.persist_remap(path, before, after) is None

    assert load_ledger(path) == after


def test_persist_refuses_a_ledger_changed_since_the_fence(tmp_path: Path) -> None:
    before = confirmed_ledger(sha=REMAP_OLD_SHA)
    path = write_ledger(tmp_path, confirmed_ledger(sha="someone-else"))

    reason = repair_ledger.persist_remap(
        path, before, before.model_copy(update={"last_clean_sha": REMAP_NEW_SHA})
    )

    assert reason is not None
    assert "changed since the fence read it" in reason


def test_persist_refuses_a_vanished_ledger(tmp_path: Path) -> None:
    before = confirmed_ledger(sha=REMAP_OLD_SHA)

    reason = repair_ledger.persist_remap(
        tmp_path / LEDGER_RELATIVE_PATH, before, before
    )

    assert reason is not None
    assert "vanished" in reason


def test_persist_refuses_an_unreadable_ledger(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    reason = repair_ledger.persist_remap(
        path, confirmed_ledger(sha=REMAP_OLD_SHA), confirmed_ledger(sha=REMAP_NEW_SHA)
    )

    assert reason is not None
    assert "unreadable before the remap write" in reason


def test_persist_reports_a_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = confirmed_ledger(sha=REMAP_OLD_SHA)
    path = write_ledger(tmp_path, before)

    def boom(_path: Path, _ledger: ConvergenceLedger) -> None:
        disk_full = "disk full"
        raise OSError(disk_full)

    monkeypatch.setattr(repair_ledger, "save_ledger", boom)

    reason = repair_ledger.persist_remap(
        path, before, before.model_copy(update={"last_clean_sha": REMAP_NEW_SHA})
    )

    assert reason is not None
    assert "write failed (OSError: disk full)" in reason


# --- engine: apply the persist -----------------------------------------------


def _job_and_state(ledger_path: Path, remap: CleanShaRemap) -> tuple[Any, Any, Any]:
    job = SimpleNamespace(
        client="client",
        ticket_id=TICKET_ID,
        fence=SimpleNamespace(ledger_path=ledger_path),
    )
    prep = SimpleNamespace(pr_number=7)
    state = SimpleNamespace(clean_remap=remap)
    return job, prep, state


def test_apply_remap_persists_and_keeps_the_passing_verdict(tmp_path: Path) -> None:
    before = confirmed_ledger(sha=REMAP_OLD_SHA)
    path = write_ledger(tmp_path, before)
    remap = clean_sha_remap()
    remapped = before.model_copy(update={"last_clean_sha": REMAP_NEW_SHA})
    verdict = FenceVerdict(True, "not_required", None, "ok", remapped, before)
    job, prep, state = _job_and_state(path, remap)

    result = engine._apply_remap(job, prep, verdict, state)

    assert result.passed is True
    assert load_ledger(path) == remapped


def test_apply_remap_fails_the_fence_when_the_write_is_refused(tmp_path: Path) -> None:
    before = confirmed_ledger(sha=REMAP_OLD_SHA)
    path = write_ledger(tmp_path, confirmed_ledger(sha="moved-on"))
    remap = clean_sha_remap()
    verdict = FenceVerdict(
        True,
        "not_required",
        None,
        "ok",
        before.model_copy(update={"last_clean_sha": REMAP_NEW_SHA}),
        before,
    )
    job, prep, state = _job_and_state(path, remap)

    result = engine._apply_remap(job, prep, verdict, state)

    assert result.passed is False
    assert result.tip_reason == "commits_after_clean_round"
    assert "ledger remap not written" in result.detail


def test_apply_remap_passes_a_verdict_without_a_remap_through(tmp_path: Path) -> None:
    verdict = FenceVerdict(True, "not_required", None, "ok")
    job, prep, state = _job_and_state(
        tmp_path / LEDGER_RELATIVE_PATH, clean_sha_remap()
    )

    assert engine._apply_remap(job, prep, verdict, state) is verdict
