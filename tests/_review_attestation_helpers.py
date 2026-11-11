"""Shared builders for the review sign-off attestation tests (#3040, RFC 0017 II1).

Plain functions and constants, no fixtures (the sibling ``_*_helpers.py``
shape). Composes on the existing shared helpers: ``sentinel_review`` owns the
sentinel envelope, ``make_review_client`` the review client,
``confirmed_ledger`` / ``write_ledger`` the convergence ledger.

Some names below are copies of private constants and fixtures of other test
modules (``GATE_LANES``, ``GATE_NOW``, ``gate_config``, the idle-router
fixtures, ``red_pr_bundle`` and the ``stamp_review_record`` /
``assert_no_review_record`` pair). Hoisting the originals onto these names is
a tracked dedupe debt item (#3040 friction report), not done here.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from click.testing import CliRunner

from cw.cli import main
from cw.dev_queue.red_pr_recovery import RedPrRecovery
from cw.models import ClientConfig, QueueItemStatus, Stage
from cw.models.branch import resolve_feature_branch
from cw.models.client import ReviewConfig
from cw.models.orchestrator_config import CLAUDE_NATIVE_BACKEND, StageExecutorConfig
from cw.models.review_attestation import AttestationCounts, ReviewAttestation
from cw.models.review_engine_summary import ReviewEngineSummary
from cw.review_engine.constants import ENGINE_STATE_RELATIVE_DIR
from cw.worktree import worktree_path_for
from tests._adopt_pr_helpers import ADOPT_PR_URL, make_repair_feedback
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._review_approval_helpers import (
    advance_main,
    attested_context,
    head,
    sync_merge,
    write_context,
)
from tests._review_convergence_helpers import (
    confirmed_ledger,
    make_review_client,
    write_ledger,
)
from tests._review_engine_helpers import SHA, make_summary, write_summary
from tests._review_history_helpers import sentinel_review
from tests._shared.git_repos import git_in
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    import pytest
    from click.testing import Result

    from cw.models import TicketTask

#: The 40-hex reviewed head every seeded review record carries.
SEEDED_REVIEW_SHA = "0123456789abcdef0123456789abcdef01234567"
REVIEW_CLIENT = "test-client"
WRITTEN_AT = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
OTHER_SHA = "b" * 40
#: The logger the attestation builder logs through.
LOGGER = "cw.dev_queue.review_attestation"


def attestation_for(head: str, **overrides: object) -> ReviewAttestation:
    """A valid engine-less PASS attestation at *head* (counts 0/0)."""
    fields: dict[str, object] = {
        "ticket_id": "T-1",
        "client": REVIEW_CLIENT,
        "head_sha": head,
        "written_at": WRITTEN_AT,
        "engine": None,
        "reviewer_backend": "claude-native",
        "verdict": "PASS",
        "counts": AttestationCounts(),
    }
    fields.update(overrides)
    return ReviewAttestation.model_validate(fields)


def review_row(worktree: Path | None, **overrides: object) -> TicketTask:
    """A RUNNING REVIEW-stage row bound to *worktree* (``None`` allowed)."""
    fields: dict[str, object] = {
        "stage": Stage.REVIEW,
        "status": QueueItemStatus.RUNNING,
        "client": REVIEW_CLIENT,
        "worktree_path": worktree,
    }
    fields.update(overrides)
    return _make_ticket_task(**fields)


def review_clients(
    worktree: Path, *, convergence: str = "clean"
) -> dict[str, ClientConfig]:
    """A one-entry clients dict built on ``make_review_client``."""
    client = make_review_client(worktree, convergence=convergence)
    return {client.name: client}


def clean_sentinel(
    reviewed_sha: str | None,
    *,
    status: str = "stage_complete",
    must_fix_initial: object = 0,
    should_fix: object = 0,
    agents_run: int = 3,
    recommendation: str = "PROCEED",
) -> dict[str, object]:
    """A clean REVIEW sentinel: success status, PROCEED health, a reviewed sha."""
    review: dict[str, object] = {
        "must_fix_initial": must_fix_initial,
        "should_fix": should_fix,
        "fix_cycles_used": 0,
        "deferred": 0,
        "agents_run": agents_run,
    }
    if reviewed_sha is not None:
        review["reviewed_sha"] = reviewed_sha
    result = sentinel_review(review, status=status)
    result["health"] = {"recommendation": recommendation}
    return result


def large_review_pending_result(reviewed_sha: str) -> dict[str, object]:
    """A measured Large-tier ``review_pending_approval`` sentinel."""
    result = clean_sentinel(reviewed_sha, status="review_pending_approval")
    result["health"] = {"recommendation": "PROCEED", "any_incomplete_risk": False}
    result["scope"] = {
        "tier": "large",
        "files": 17,
        "lines_estimate": 10,
        "lines_actual": 1280,
        "forbidden_touched": False,
    }
    return result


def converged_worktree_ledger(worktree: Path, sha: str, ticket_id: str) -> Path:
    """Write a ledger converged at *sha* for *ticket_id* into *worktree*."""
    return write_ledger(worktree, confirmed_ledger(sha=sha, ticket_id=ticket_id))


def clean_client_wt(
    tmp_path: Path, ticket_id: str, *, convergence: str = "clean"
) -> tuple[ClientConfig, Path]:
    """A client with a ``worktree_base`` and its created, git-less worktree dir."""
    base = make_review_client(tmp_path / "ws", convergence=convergence)
    client = base.model_copy(update={"worktree_base": tmp_path / "wts"})
    worktree = worktree_path_for(client, resolve_feature_branch(ticket_id, client))
    worktree.mkdir(parents=True, exist_ok=True)
    return client, worktree


def stamp_review_record(task: TicketTask, sha: str = SEEDED_REVIEW_SHA) -> TicketTask:
    """Seed the v48 approval trio and an attestation at *sha* onto *task*."""
    task.review_approved_at = datetime(2026, 10, 25, tzinfo=UTC)
    task.review_approver_kind = "operator"
    task.review_approved_sha = sha
    task.review_attestation = attestation_for(
        sha, ticket_id=task.ticket_id, client=task.client
    )
    return task


def assert_no_review_record(task: TicketTask) -> None:
    """The approval trio and the attestation are all cleared."""
    assert task.review_approved_at is None
    assert task.review_approver_kind is None
    assert task.review_approved_sha is None
    assert task.review_attestation is None


def red_pr_bundle(**overrides: object) -> RedPrRecovery:
    """A ``RedPrRecovery`` bundle (copy of ``test_red_pr_recovery._bundle``)."""
    bundle = RedPrRecovery(
        pr_url=ADOPT_PR_URL,
        repo="acme/widgets",
        pr_number=7,
        head_sha="b" * 40,
        checks=("test (macos-latest, 3.13)",),
        feedback=make_repair_feedback(reviewed_sha="b" * 40),
        was_armed=True,
        stages=(Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE),
        backend=CLAUDE_NATIVE_BACKEND,
        lane="default",
    )
    return bundle._replace(**overrides)


ATTEST_TICKET = "T-1"
ATTEST_MIGRATION = "alembic/versions/0001_x.py"


class AttestedSetup:
    """A feature worktree attested at ``attested``, with its context written.

    Hoisted from ``test_cli_review_attestation.py`` (#3043) so the sign-off CLI
    tests share one fixture instead of a private copy.
    """

    def __init__(self, worktree: Path, origin: Path, side: Path) -> None:
        self.worktree = worktree
        self.origin = origin
        self.side = side
        self.attested = head(worktree)

    def commit(self, rel: str, text: str) -> str:
        path = self.worktree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git_in(self.worktree, "add", rel)
        git_in(self.worktree, "commit", "-m", f"edit {rel}")
        return head(self.worktree)

    def rename(self, old: str, new: str) -> str:
        git_in(self.worktree, "mv", old, new)
        git_in(self.worktree, "commit", "-m", "rename")
        return head(self.worktree)

    def merge_main(self) -> str:
        advance_main(self.origin, self.worktree, self.side, "main_only.py")
        sync_merge(self.worktree)
        return head(self.worktree)

    def live_client(self, review: dict[str, object] | None) -> None:
        extra: dict[str, object] = {} if review is None else {"review": review}
        write_clients_yaml(
            ClientSpec(REVIEW_CLIENT, self.worktree, default_branch="main", extra=extra)
        )

    def ledger(self, sha: str | None = None) -> None:
        converged_worktree_ledger(self.worktree, sha or self.attested, ATTEST_TICKET)

    def invoke(self, command: Sequence[str], *extra: str) -> Result:
        return CliRunner().invoke(
            main,
            [*command, "--worktree", str(self.worktree), *extra],
        )

    def run(self, *extra: str) -> Result:
        return self.invoke(("review", "attestation", "check"), *extra)


def write_attested(
    setup: AttestedSetup,
    *,
    attestation_overrides: dict[str, object] | None = None,
    **context_overrides: object,
) -> None:
    task = review_row(setup.worktree)
    att = attestation_for(setup.attested, **(attestation_overrides or {}))
    overrides: dict[str, object] = {"ticket_id": ATTEST_TICKET, "client": REVIEW_CLIENT}
    overrides.update(context_overrides)
    payload = attested_context(setup.worktree, task, att, **overrides)
    write_context(setup.worktree, payload)


def build_attested_setup(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    name: str,
) -> AttestedSetup:
    """A repo with CHANGELOG and a migration committed, attested at HEAD."""
    worktree, origin = make_git_repo_with_origin(name)
    built = AttestedSetup(worktree, origin, tmp_path / "side")
    built.commit("CHANGELOG.md", "# Changelog\n")
    built.commit(ATTEST_MIGRATION, 'revision = "a1"\ndown_revision = None\nbody = 1\n')
    built.attested = head(worktree)
    write_attested(built)
    return built


def with_engine_config(client: ClientConfig) -> ClientConfig:
    """*client* with a clean-convergence ``review.engine`` of ``code-review``."""
    review = ReviewConfig.model_validate(
        {"convergence": "clean", "engine": {"skill": "code-review"}}
    )
    return client.model_copy(update={"review": review})


def engine_client(tmp_path: Path) -> ClientConfig:
    """A clean-mode review client with an engine configured."""
    return with_engine_config(make_review_client(tmp_path))


def with_review_backend(client: ClientConfig, backend: str) -> ClientConfig:
    """*client* with the REVIEW executor backend set to *backend*."""
    pipeline = client.pipeline.model_copy(
        update={"executors": {Stage.REVIEW: StageExecutorConfig(backend=backend)}}
    )
    return client.model_copy(update={"pipeline": pipeline})


def clean_engine_summary(**overrides: object) -> ReviewEngineSummary:
    """A valid PASS/COMPLETE engine summary at ``SHA`` with acceptance and a gate."""
    raw = make_summary(
        head_sha=SHA,
        acceptance=[
            {"id": "AC-2", "status": "PASS", "evidence": None},
            {"id": "AC-1", "status": "NOT_APPLICABLE", "evidence": None},
        ],
        gate_signals=[{"name": "tests", "status": "PASS", "detail": None}],
    )
    raw.update(overrides)
    return ReviewEngineSummary.model_validate(raw)


def write_round_summary(
    worktree: Path, round_id: str, summary: dict[str, object] | bytes
) -> Path:
    """Write *summary* as ``<worktree>/.cw/review-engine/<round_id>/summary.json``."""
    return write_summary(worktree / ENGINE_STATE_RELATIVE_DIR / round_id, summary)


class NoSubprocessError(AssertionError):
    """Raised by :func:`forbidden_call` when the builder shells out."""


def forbidden_call(*_args: object, **_kwargs: object) -> object:
    raise NoSubprocessError


def install_no_git_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire git and every subprocess entry point to fail the test."""
    monkeypatch.setattr("cw._git.run_git", forbidden_call)
    monkeypatch.setattr("cw.dispatch.review_gates.current_head_sha", forbidden_call)
    monkeypatch.setattr("cw.worktree._paths._checked_out_branch", forbidden_call)
    monkeypatch.setattr(subprocess, "run", forbidden_call)
    monkeypatch.setattr(subprocess, "Popen", forbidden_call)
