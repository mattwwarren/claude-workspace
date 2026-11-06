"""The repair recipe's lease push advances an adopted row's pin (RFC 0016 R5, #2943).

``_finish_steps`` opens with ``advance_pin_after_repair``: the second of the
two automatic pin advances. Driven through the real ``_rewrite_and_push`` and
``_finish_repair`` with the push, the fence and the arm stubbed at the engine
seam; the dev queue is real.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import DevQueueStore, QueueItemStatus
from cw.pr_unarm import UnarmOutcome
from cw.reconcile.review_recipes import repair_engine as engine
from cw.reconcile.review_recipes.repair_engine import (
    Prepared,
    RepairState,
    Staged,
)
from cw.reconcile.review_recipes.repair_fence import FenceVerdict
from cw.reconcile.review_recipes.repair_git import RepairStopError
from cw.repair_disposition import RepairDisposition
from tests._changelog_repair_helpers import (
    EXPECTED_SHA,
    NEW_SHA,
    PR_NUMBER,
    make_client,
    make_repair_job,
    repair_task,
)

if TYPE_CHECKING:
    from cw.models import ClientConfig, TicketTask
    from cw.reconcile.review_recipes.repair_engine import RepairJob

_ADOPTED_BRANCH = "feature/ext-pr"
_ADOPTED_PR = "https://github.com/acme/widgets/pull/7"
_ADOPTION_SRC = Path(__file__).resolve().parents[1] / "src/cw/dev_queue/adoption.py"


@pytest.fixture
def client(tmp_path: Path) -> ClientConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    return make_client(repo)


def _adopted_row(**fields: Any) -> TicketTask:
    values: dict[str, Any] = {
        "branch": _ADOPTED_BRANCH,
        "adopted_head_sha": EXPECTED_SHA,
        "adopted_pr_url": _ADOPTED_PR,
        "pr_url": _ADOPTED_PR,
        "adopted_repin_due": True,
    }
    values.update(fields)
    return repair_task(**values)


def _job(client: ClientConfig, row: TicketTask) -> RepairJob:
    save_dev_queue(DevQueueStore(tasks=[row]))
    job = make_repair_job(client, task=row)
    spec = dataclasses.replace(job.spec, rewrite=lambda *_a: NEW_SHA)
    return job._replace(spec=spec)


def _prep(client: ClientConfig) -> Prepared:
    return Prepared(
        branch=_ADOPTED_BRANCH,
        repo=Path(client.workspace_path),
        pr_number=int(PR_NUMBER),
        default_branch="main",
        default_ref="origin/main",
        expected_sha=EXPECTED_SHA,
        merge_base="c" * 40,
        conflicts=frozenset(),
    )


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _record(*_a: object, **_kw: object) -> None:
        calls.append("pr_action_taken")

    def _push(_scratch: Path, branch: str, expected: str, new: str) -> None:
        calls.append(f"push {branch} {expected}->{new}")

    monkeypatch.setattr(engine, "_disarm_before_push", lambda *_a: None)
    monkeypatch.setattr(engine, "_push_with_lease", _push)
    monkeypatch.setattr(engine, "_record_pr_action_taken", _record)
    monkeypatch.setattr(engine, "_ls_remote_tip", lambda *_a: NEW_SHA)
    monkeypatch.setattr(
        engine,
        "evaluate_repair_fence",
        lambda *_a, **_kw: FenceVerdict(True, "tok", None, "ok"),
    )
    monkeypatch.setattr(engine, "_arm", lambda job, *_a: job.ticket_id)
    return calls


def _run(job: RepairJob, prep: Prepared, tmp_path: Path) -> str:
    staged: Staged[Any] = Staged(arm_script=tmp_path / "arm.sh", summary="", data=None)
    state = RepairState(job)
    new_sha = engine._rewrite_and_push(job, prep, staged, tmp_path, state)
    return engine._finish_repair(job, prep, staged, tmp_path, state, new_sha)


@pytest.mark.usefixtures("seams")
def test_successful_push_advances_the_adopted_pin(
    client: ClientConfig, tmp_path: Path
) -> None:
    job = _job(client, _adopted_row())
    assert _run(job, _prep(client), tmp_path) == job.ticket_id
    row = load_dev_queue().tasks[0]
    assert row.adopted_head_sha == NEW_SHA
    assert row.adopted_repin_due is False


def test_failed_push_leaves_the_pin(
    client: ClientConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seams: list[str],
) -> None:
    def _rejected(*_a: object) -> None:
        raise RepairStopError(RepairDisposition.PUSH_REJECTED, "lease rejected")

    monkeypatch.setattr(engine, "_push_with_lease", _rejected)
    job = _job(client, _adopted_row())
    with pytest.raises(RepairStopError):
        _run(job, _prep(client), tmp_path)
    assert load_dev_queue().tasks[0].adopted_head_sha == EXPECTED_SHA
    assert "pr_action_taken" not in seams


@pytest.mark.usefixtures("seams")
def test_non_adopted_row_is_untouched(client: ClientConfig, tmp_path: Path) -> None:
    job = _job(client, repair_task())
    assert _run(job, _prep(client), tmp_path) == job.ticket_id
    row = load_dev_queue().tasks[0]
    assert row.adopted_head_sha is None
    assert row.adopted_repin_due is False


@pytest.mark.usefixtures("seams")
def test_crash_in_pin_write_is_post_push_failed(
    client: ClientConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: object, **_kw: object) -> bool:
        msg = "queue write failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(engine, "advance_pin_after_repair", _boom)
    monkeypatch.setattr(
        engine, "ensure_pr_unarmed", lambda *_a, **_kw: UnarmOutcome(True, False, "")
    )
    job = _job(client, _adopted_row())
    with pytest.raises(RepairStopError) as exc:
        _run(job, _prep(client), tmp_path)
    assert exc.value.disposition == RepairDisposition.POST_PUSH_FAILED


def test_adoption_policy_never_imports_the_repair_fence() -> None:
    tree = ast.parse(_ADOPTION_SRC.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any("repair_fence" in name for name in imported)


@pytest.mark.parametrize("repin_due", [True, False])
def test_is_repair_shape_ignores_adoption(repin_due: bool) -> None:
    """Shape detection is unchanged by adoption (RFC 0016 D-6, resolution 13).

    An adopted COMPLETED CONFLICTING/DIRTY merge_blocked row is repair-shaped
    exactly as its non-adopted twin; the repair path is out of scope here.
    """
    plain = repair_task()
    adopted = _adopted_row(adopted_repin_due=repin_due)
    assert engine.is_repair_shape(plain) is True
    assert engine.is_repair_shape(adopted) is engine.is_repair_shape(plain)


def test_is_repair_shape_still_rejects_a_non_conflicting_adopted_row() -> None:
    adopted = _adopted_row(status=QueueItemStatus.RUNNING)
    assert engine.is_repair_shape(adopted) is engine.is_repair_shape(
        repair_task(status=QueueItemStatus.RUNNING)
    )
    assert engine.is_repair_shape(adopted) is False
