"""Real-git acceptance for the scope-drift carry (#3772).

A grant approved at H1 must survive a descendant H2: the carried transport
re-populates the approved paths, and the real scope-conformance script then
presents only the genuinely new path. Negatives leave the transport unset and
log a reason, so the gate lists every touched path, as it did before the ledger
existed.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cw import _git as cw_git
from cw.auto_dev_result import PLAN_SCOPE_DRIFT_BLOCKER_REASON
from cw.models.client import ClientConfig
from cw.models.enums import Stage
from cw.scope_grants import (
    carry_scope_grants,
    parse_drift_extra_files,
    review_policy_fingerprint,
)
from cw.stage_verification.impl_gates import run_scope_conformance
from tests._shared.git_repos import git_in
from tests._shared.scope_grant_builders import (
    GRANT_CLIENT,
    GRANT_SLUG,
    GrantRepo,
    build_grant_repo,
    grant_task,
    install_real_scope_script,
    make_grant,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models.scope_grant import ScopeGrant
    from cw.models.tasks.ticket_task import TicketTask

_PLAN = "## Files Modified\n\n- src/plan.py\n"
_TOUCHED = ["src/plan.py", "src/a.py", "src/b.py", "src/c.py"]
_TRANSPORT_FIELDS = {"scope_drift_approved_extra_files", "scope_drift_approved_head"}
_LOGGER = "cw.scope_grants"


def _client(repo: Path, tmp_path: Path) -> ClientConfig:
    """A minimal GitHub-backed client rooted at *repo*."""
    return ClientConfig(
        name=GRANT_CLIENT,
        workspace_path=repo,
        default_branch="main",
        worktree_base=tmp_path / "worktrees",
        blocked_result_requeue_enabled=True,
    )


def _grant(config: ClientConfig, head: str, **overrides: object) -> ScopeGrant:
    """A grant for a and b at *head* that binds to *config*'s review policy."""
    fields: dict[str, object] = {
        "head": head,
        "policy": review_policy_fingerprint(config.review),
    }
    fields.update(overrides)
    return make_grant(**fields)


def _write_plan(repo: Path) -> Path:
    """The plan lives inside the repo: the script reads thresholds near it."""
    plan = repo / ".cw" / "plan.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(_PLAN, encoding="utf-8")
    return plan


def _gate(repo: Path, task: TicketTask, scratch: Path) -> str:
    block, friction = run_scope_conformance(
        repo,
        task=task,
        plan_text=_PLAN,
        plan_path=_write_plan(repo),
        touched=_TOUCHED,
        scratch=scratch,
    )
    assert block is not None, friction
    assert block.reason == PLAN_SCOPE_DRIFT_BLOCKER_REASON
    return block.details


@pytest.fixture
def grant_repo(make_git_repo: Callable[..., Path]) -> GrantRepo:
    install_real_scope_script()
    return build_grant_repo(make_git_repo)


class TestVerifiedDescendantCarry:
    def test_carry_presents_only_the_new_path(
        self, grant_repo: GrantRepo, tmp_path: Path
    ) -> None:
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task([_grant(client, grant_repo.h1)])

        carry_scope_grants(task, client, grant_repo.worktree, repo=GRANT_SLUG)

        assert task.scope_drift_approved_extra_files == ["src/a.py", "src/b.py"]
        assert task.scope_drift_approved_head == grant_repo.h1
        details = _gate(grant_repo.worktree, task, tmp_path / "scratch")
        assert "- src/c.py" in details
        assert "- src/a.py" not in details
        assert "- src/b.py" not in details
        # The real producer's text must parse back to exactly the new path.
        assert parse_drift_extra_files(details) == ["src/c.py"]

    def test_without_carry_the_gate_lists_every_extra_path(
        self, grant_repo: GrantRepo, tmp_path: Path
    ) -> None:
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task([_grant(client, grant_repo.h1)])

        details = _gate(grant_repo.worktree, task, tmp_path / "scratch")

        for path in ("src/a.py", "src/b.py", "src/c.py"):
            assert f"- {path}" in details
        assert parse_drift_extra_files(details) == ["src/a.py", "src/b.py", "src/c.py"]

    def test_carry_authorizes_paths_only(
        self, grant_repo: GrantRepo, tmp_path: Path
    ) -> None:
        """Nothing but the two transport fields may change (no approval bypass)."""
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task([_grant(client, grant_repo.h1)])
        before = task.model_dump(mode="json")

        carry_scope_grants(task, client, grant_repo.worktree, repo=GRANT_SLUG)

        after = task.model_dump(mode="json")
        changed = {key for key in after if after[key] != before[key]}
        assert changed == _TRANSPORT_FIELDS
        for untouched in (
            "signoff",
            "hold_finalize",
            "review_approved_at",
            "review_approved_head",
            "plan_approved_at",
            "plan_approved_fingerprint",
            "must_fix_override",
            "review_attestation",
        ):
            if untouched in before:
                assert after[untouched] == before[untouched], untouched


class TestNegativesFailClosed:
    """Each negative leaves the transport unset, logs a reason, lists a, b, c."""

    @staticmethod
    def _assert_not_carried(
        grant_repo: GrantRepo,
        client: ClientConfig,
        task: TicketTask,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
        reasons: tuple[str, ...],
        *,
        slug: str | None = GRANT_SLUG,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            carry_scope_grants(task, client, grant_repo.worktree, repo=slug)
        assert task.scope_drift_approved_extra_files is None
        assert task.scope_drift_approved_head is None
        logged = " ".join(record.getMessage() for record in caplog.records)
        assert any(reason in logged for reason in reasons), logged
        details = _gate(grant_repo.worktree, task, tmp_path / "scratch")
        for path in ("src/a.py", "src/b.py", "src/c.py"):
            assert f"- {path}" in details

    @pytest.mark.parametrize(
        "override",
        [
            {"row_created_at": datetime(2026, 11, 9, tzinfo=UTC)},
            {"ticket_id": "T-9"},
            {"client": "other-client"},
        ],
        ids=["created_at", "ticket", "client"],
    )
    def test_row_mismatch(
        self,
        override: dict[str, object],
        grant_repo: GrantRepo,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task([_grant(client, grant_repo.h1, **override)])

        self._assert_not_carried(
            grant_repo, client, task, caplog, tmp_path, ("row identity differs",)
        )

    def test_pr_both_known_and_differing(
        self, grant_repo: GrantRepo, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task(
            [_grant(client, grant_repo.h1, pr_number=7)],
            pr_url="https://github.com/acme/widgets/pull/8",
        )

        self._assert_not_carried(
            grant_repo, client, task, caplog, tmp_path, ("pull request differs",)
        )

    def test_pr_known_on_one_side_only_still_carries(
        self, grant_repo: GrantRepo, tmp_path: Path
    ) -> None:
        """The same wiring, matching PR number: ``task.pr_url`` reaches the verifier."""
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task(
            [_grant(client, grant_repo.h1, pr_number=8)],
            pr_url="https://github.com/acme/widgets/pull/8",
        )

        carry_scope_grants(task, client, grant_repo.worktree, repo=GRANT_SLUG)

        assert task.scope_drift_approved_head == grant_repo.h1

    def test_changed_review_policy(
        self, grant_repo: GrantRepo, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task([_grant(client, grant_repo.h1, policy="stale-policy")])

        self._assert_not_carried(
            grant_repo, client, task, caplog, tmp_path, ("review policy changed",)
        )

    @pytest.mark.parametrize(
        ("granted", "current"),
        [("X", "Y"), (None, "X"), ("X", None)],
        ids=["x-to-y", "none-to-x", "x-to-none"],
    )
    def test_changed_plan_fingerprint(
        self,
        granted: str | None,
        current: str | None,
        grant_repo: GrantRepo,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task(
            [_grant(client, grant_repo.h1, plan_fingerprint=granted)],
            plan_approved_fingerprint=current,
        )

        self._assert_not_carried(
            grant_repo, client, task, caplog, tmp_path, ("plan of record changed",)
        )

    def test_repo_mismatch(
        self, grant_repo: GrantRepo, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task([_grant(client, grant_repo.h1)])

        self._assert_not_carried(
            grant_repo,
            client,
            task,
            caplog,
            tmp_path,
            ("repository differs",),
            slug="acme/other",
        )

    def test_repo_unresolved(
        self, grant_repo: GrantRepo, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task([_grant(client, grant_repo.h1)])

        self._assert_not_carried(
            grant_repo,
            client,
            task,
            caplog,
            tmp_path,
            ("repository unbound",),
            slug=None,
        )

    def test_grant_without_a_repo_binding(
        self, grant_repo: GrantRepo, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task([_grant(client, grant_repo.h1, repo=None)])

        self._assert_not_carried(
            grant_repo, client, task, caplog, tmp_path, ("repository unbound",)
        )

    def test_rewritten_history(
        self, grant_repo: GrantRepo, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task([_grant(client, grant_repo.h1)])
        # An unrelated root commit that still touches a, b and c: H1 is no
        # longer an ancestor of HEAD, so the grant must not be reused.
        git_in(grant_repo.worktree, "checkout", "-q", "--orphan", "rewritten")
        git_in(grant_repo.worktree, "commit", "-q", "-m", "rewrite")

        self._assert_not_carried(
            grant_repo,
            client,
            task,
            caplog,
            tmp_path,
            ("head is not an ancestor of HEAD", "ancestry unverifiable"),
        )

    def test_absent_object(
        self, grant_repo: GrantRepo, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A well-formed 40-hex head that the repository does not hold."""
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task([_grant(client, "f" * 40)])

        self._assert_not_carried(
            grant_repo,
            client,
            task,
            caplog,
            tmp_path,
            ("head is not an ancestor of HEAD", "ancestry unverifiable"),
        )

    def test_non_impl_stage_is_a_no_op(
        self, grant_repo: GrantRepo, tmp_path: Path
    ) -> None:
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task([_grant(client, grant_repo.h1)], stage=Stage.REVIEW)

        carry_scope_grants(task, client, grant_repo.worktree, repo=GRANT_SLUG)

        assert task.scope_drift_approved_extra_files is None
        assert task.scope_drift_approved_head is None

    def test_empty_ledger_leaves_a_legacy_transport_untouched(
        self, grant_repo: GrantRepo, tmp_path: Path
    ) -> None:
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task(
            [],
            scope_drift_approved_extra_files=["src/a.py"],
            scope_drift_approved_head=grant_repo.h1,
        )

        carry_scope_grants(task, client, grant_repo.worktree, repo=GRANT_SLUG)

        assert task.scope_drift_approved_extra_files == ["src/a.py"]
        assert task.scope_drift_approved_head == grant_repo.h1

    def test_git_timeout_never_raises(
        self,
        grant_repo: GrantRepo,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client = _client(grant_repo.worktree, tmp_path)
        task = grant_task([_grant(client, grant_repo.h1)])

        def _timeout(*_args: str) -> bool | None:
            raise subprocess.TimeoutExpired(cmd="git", timeout=5)

        monkeypatch.setattr(cw_git, "is_ancestor", _timeout)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            carry_scope_grants(task, client, grant_repo.worktree, repo=GRANT_SLUG)

        assert task.scope_drift_approved_extra_files is None
        assert "verification unavailable" in caplog.text
