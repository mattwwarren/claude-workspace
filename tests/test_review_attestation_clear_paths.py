"""Every revoke path clears the attestation with the approval (#3040, RFC 0017 II1).

Each test seeds the approval trio plus an attestation (``stamp_review_record``),
drives one entry point, and asserts both are gone and exactly one
``review.attestation_cleared`` event names the right ``path`` and
``stage_after`` (the stage the row holds after the revoke).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.dev_queue import ADOPTED_HEAD_DRIFT_DISPOSITION
from cw.dev_queue.lifecycle import _stage_regress
from cw.dev_queue.red_pr_recovery import recover_red_pr_ticket
from cw.dev_queue.requeue import requeue_ticket
from cw.dispatch import apply_staged_decision
from cw.models import ClientConfig, OrchestratorEventType, QueueItemStatus, Stage
from cw.native_daemon import FakeNativeDaemonClient
from tests._adopt_pr_helpers import (
    ADOPT_CLIENT,
    adopt_client,
    make_adopted_task,
    make_pinned_origin,
    make_repair_feedback,
)
from tests._clients_yaml import write_clients_yaml
from tests._review_attestation_helpers import (
    SEEDED_REVIEW_SHA,
    assert_no_review_record,
    red_pr_bundle,
    stamp_review_record,
)
from tests._shipped_red_helpers import make_red_row
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    events_of,
    install_acme_client,
    make_blocked,
    make_task,
    queue_row,
    seed_queue,
)
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import OrchestratorEvent, TicketTask

CLEARED = OrchestratorEventType.REVIEW_ATTESTATION_CLEARED
_SRC = Path(__file__).resolve().parents[1] / "src" / "cw"


def _one_cleared(path: str, stage_after: str, head: str = SEEDED_REVIEW_SHA) -> None:
    events: list[OrchestratorEvent] = events_of(CLEARED)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["head_sha"] == head
    assert payload["path"] == path
    assert payload["stage_after"] == stage_after
    assert "stage" not in payload
    assert set(payload) == {
        "ticket_id",
        "client",
        "head_sha",
        "engine",
        "reviewer_backend",
        "path",
        "stage_after",
    }


@pytest.mark.parametrize("target", [Stage.IMPL, Stage.PLAN, Stage.REVIEW])
def test_stage_regress_direct(tmp_config_dir: Path, target: Stage) -> None:
    row = stamp_review_record(
        make_task(stage=Stage.REVIEW, status=QueueItemStatus.RUNNING)
    )
    _stage_regress(row, target)
    assert_no_review_record(row)
    _one_cleared("stage_regress", target.value)


@pytest.mark.parametrize("target", ["plan", "impl"])
def test_requeue_backward(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    daemon = install_acme_client(tmp_path, monkeypatch)
    seed_queue([stamp_review_record(make_blocked(stage=Stage.REVIEW))])
    requeue_ticket(
        ACME_TICKET,
        ACME,
        stage_override=target,
        allow_regress=True,
        native_daemon=daemon,
    )
    assert_no_review_record(queue_row())
    _one_cleared("stage_regress", target)


def test_requeue_with_review_feedback(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = install_acme_client(tmp_path, monkeypatch)
    seed_queue([stamp_review_record(make_blocked(stage=Stage.REVIEW))])
    feedback = make_repair_feedback()
    requeue_ticket(
        ACME_TICKET,
        ACME,
        stage_override="impl",
        allow_regress=True,
        native_daemon=daemon,
        review_feedback=feedback,
    )
    row = queue_row()
    assert_no_review_record(row)
    assert row.review_feedback == feedback
    _one_cleared("stage_regress", "impl")


@pytest.mark.parametrize(
    ("extra", "path", "stage_after"),
    [
        ({}, "same_stage_requeue", "review"),
        ({"stage_override": "impl", "allow_regress": True}, "stage_regress", "impl"),
    ],
    ids=["default_review_target", "backward_repin"],
)
def test_adopted_repin(
    tmp_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    extra: dict[str, object],
    path: str,
    stage_after: str,
) -> None:
    pinned = make_pinned_origin(tmp_path, make_git_repo_with_origin)
    write_clients_yaml(adopt_client(pinned.repo))
    row = make_adopted_task(
        ticket_id="T-1",
        branch=pinned.branch,
        adopted_head_sha=pinned.pin,
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition=ADOPTED_HEAD_DRIFT_DISPOSITION,
        adopted_repin_due=True,
    )
    seed_queue([stamp_review_record(row, pinned.pin)])
    head = pinned.push_descendant("foreign.py")
    git_in(pinned.repo, "fetch", "origin")
    monkeypatch.setattr(
        "cw.dev_queue.adoption.branch_head_sha_on_origin",
        lambda _branch, **_kw: (head, True),
    )
    monkeypatch.setattr(
        "cw.dev_queue.requeue.get_native_daemon_client", FakeNativeDaemonClient
    )
    stage_override = extra.get("stage_override")
    assert stage_override is None or isinstance(stage_override, str)
    requeue_ticket(
        "T-1",
        ADOPT_CLIENT,
        stage_override,
        allow_regress=extra.get("allow_regress") is True,
        repin_adopted_head=True,
    )
    assert_no_review_record(queue_row("T-1"))
    _one_cleared(path, stage_after, head=pinned.pin)


def test_recover_red_pr(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = install_acme_client(tmp_path, monkeypatch)
    seed_queue([stamp_review_record(make_red_row(alerted=True))])
    recover_red_pr_ticket(ACME_TICKET, ACME, red_pr_bundle(), native_daemon=daemon)
    assert_no_review_record(queue_row())
    _one_cleared("stage_regress", "impl")


def test_rule_5a_finalize_self_heal(tmp_config_dir: Path, tmp_path: Path) -> None:
    row = stamp_review_record(
        make_task(stage=Stage.FINALIZE, status=QueueItemStatus.RUNNING)
    )
    clients = {ACME: ClientConfig(name=ACME, workspace_path=tmp_path)}
    last_result: dict[str, object] = {
        "status": "blocked",
        "blocker": {"stage": "s4_finalize", "reason": "agent_block"},
    }
    apply_staged_decision(row, "blocked", last_result, clients)
    assert_no_review_record(row)
    _one_cleared("stage_regress", "impl")


def test_same_stage_review_requeue(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = install_acme_client(tmp_path, monkeypatch)
    seed_queue([stamp_review_record(make_blocked(stage=Stage.REVIEW))])
    requeue_ticket(ACME_TICKET, ACME, "review", native_daemon=daemon)
    row = queue_row()
    assert row.stage == Stage.REVIEW
    assert_no_review_record(row)
    _one_cleared("same_stage_requeue", "review")


def _assert_kept(row: TicketTask) -> None:
    assert row.review_approved_sha == SEEDED_REVIEW_SHA
    assert row.review_attestation is not None
    assert row.review_attestation.head_sha == SEEDED_REVIEW_SHA
    assert events_of(CLEARED) == []


def test_forward_requeue_keeps_attestation(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = install_acme_client(tmp_path, monkeypatch)
    seed_queue([stamp_review_record(make_blocked(stage=Stage.REVIEW))])
    requeue_ticket(ACME_TICKET, ACME, "finalize", native_daemon=daemon)
    _assert_kept(queue_row())


def test_finalize_same_stage_requeue_keeps_attestation(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = install_acme_client(tmp_path, monkeypatch)
    seed_queue([stamp_review_record(make_blocked(stage=Stage.FINALIZE))])
    requeue_ticket(ACME_TICKET, ACME, native_daemon=daemon)
    _assert_kept(queue_row())


# ---------------------------------------------------------------------------
# Single-writer rule: the builder and the clear are the only writers.
# ---------------------------------------------------------------------------

_ALLOWED_WRITERS = {
    ("dev_queue/review_attestation.py", None),
    ("dev_queue/lifecycle.py", "_clear_review_approval"),
}


def _writers_in(source: str) -> set[str | None]:
    """Names of the functions in *source* that assign ``.review_attestation``."""
    tree = ast.parse(source)
    found: set[str | None] = set()

    def visit(node: ast.AST, func: str | None) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            func = node.name
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "review_attestation"
            ):
                found.add(func)
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "review_attestation"
        ):
            found.add(func)
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    visit(tree, None)
    return found


def test_only_the_builder_and_the_clear_write_the_attestation() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        for func in _writers_in(path.read_text(encoding="utf-8")):
            if (rel, None) in _ALLOWED_WRITERS or (rel, func) in _ALLOWED_WRITERS:
                continue
            offenders.append(f"{rel}:{func}")
    assert offenders == []


@pytest.mark.parametrize(
    "source",
    [
        "def f(task):\n    task.review_attestation = None\n",
        "def f(task):\n    setattr(task, 'review_attestation', None)\n",
        "def f(task):\n    task.review_attestation: object = None\n",
    ],
)
def test_writer_scan_catches_a_planted_writer(source: str) -> None:
    """Negative control: a deliberately added writer is found."""
    assert _writers_in(source) == {"f"}
