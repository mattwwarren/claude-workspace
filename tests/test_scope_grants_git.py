"""Real-git acceptance for the scope-drift carry (#3772).

A grant approved at H1 must survive a descendant H2: the carried transport
re-populates the approved paths, and the real scope-conformance script then
presents only the genuinely new path. Negatives leave the transport unset so the
gate lists every touched path, as it did before the ledger existed.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw import _git as cw_git
from cw.auto_dev_result import PLAN_SCOPE_DRIFT_BLOCKER_REASON
from cw.models.client import ClientConfig
from cw.models.enums import Stage
from cw.models.scope_grant import ScopeGrant
from cw.scope_grants import carry_scope_grants, review_policy_fingerprint
from cw.stage_verification.impl_gates import run_scope_conformance
from tests._shared.git_repos import git_in
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from cw.models.tasks.ticket_task import TicketTask

_REAL_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "scripts"
    / "check_plan_scope_conformance.py"
)
_ROW_CREATED = datetime(2026, 11, 2, tzinfo=UTC)
_REPO_SLUG = "acme/widgets"
_PLAN = "## Files Modified\n\n- src/plan.py\n"
_TOUCHED = ["src/plan.py", "src/a.py", "src/b.py", "src/c.py"]
_PYPROJECT = "[tool.cw.scope_conformance]\nabs_floor = 0\n"


def _client(repo: Path, tmp_path: Path) -> ClientConfig:
    """A minimal GitHub-backed client rooted at *repo*."""
    return ClientConfig(
        name="test-client",
        workspace_path=repo,
        default_branch="main",
        worktree_base=tmp_path / "worktrees",
        blocked_result_requeue_enabled=True,
    )


def _write(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _head(repo: Path) -> str:
    return git_in(repo, "rev-parse", "HEAD").strip()


def _install_real_script() -> None:
    """Install the repo's real scope script under the per-test HOME."""
    target_dir = Path.home() / ".claude" / "scripts"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / _REAL_SCRIPT.name).write_bytes(_REAL_SCRIPT.read_bytes())


def _build_repo(tmp_path: Path) -> tuple[Path, str]:
    """H1 adds a.py and b.py; H2 (HEAD) adds c.py. Returns (repo, H1)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git_in(repo, "init", "-q", "-b", "main")
    _write(repo, "pyproject.toml", _PYPROJECT)
    _write(repo, "src/a.py", "a = 1\n")
    _write(repo, "src/b.py", "b = 1\n")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-q", "-m", "H1")
    h1 = _head(repo)
    _write(repo, "src/c.py", "c = 1\n")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-q", "-m", "H2")
    return repo, h1


def _grant(client: ClientConfig, head: str) -> ScopeGrant:
    return ScopeGrant(
        client=client.name,
        ticket_id="T-1",
        row_created_at=_ROW_CREATED,
        repo=_REPO_SLUG,
        pr_number=None,
        head=head,
        policy=review_policy_fingerprint(client.review),
        plan_fingerprint=None,
        paths=["src/a.py", "src/b.py"],
        actor="op",
        recorded_at=datetime(2026, 11, 3, tzinfo=UTC),
    )


def _task(client: ClientConfig, grants: list[ScopeGrant]) -> TicketTask:
    return _make_ticket_task(
        ticket_id="T-1",
        client=client.name,
        stage=Stage.IMPL,
        created_at=_ROW_CREATED,
        scope_grants=grants,
    )


def _write_plan(repo: Path) -> Path:
    """The plan lives inside the repo: the script reads thresholds near it."""
    plan = repo / ".cw" / "plan.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(_PLAN, encoding="utf-8")
    return plan


def _gate(repo: Path, task: TicketTask, plan: Path, scratch: Path) -> str:
    block, friction = run_scope_conformance(
        repo,
        task=task,
        plan_text=_PLAN,
        plan_path=plan,
        touched=_TOUCHED,
        scratch=scratch,
    )
    assert block is not None, friction
    assert block.reason == PLAN_SCOPE_DRIFT_BLOCKER_REASON
    return block.details


class TestVerifiedDescendantCarry:
    def test_carry_presents_only_the_new_path(self, tmp_path: Path) -> None:
        repo, h1 = _build_repo(tmp_path)
        _install_real_script()
        client = _client(repo, tmp_path)
        task = _task(client, [_grant(client, h1)])
        plan = _write_plan(repo)

        carry_scope_grants(task, client, repo, repo=_REPO_SLUG)

        assert task.scope_drift_approved_extra_files == ["src/a.py", "src/b.py"]
        assert task.scope_drift_approved_head == h1
        details = _gate(repo, task, plan, tmp_path / "scratch")
        assert "touches 1 file(s)" in details

    def test_without_carry_the_gate_lists_every_extra_path(
        self, tmp_path: Path
    ) -> None:
        repo, h1 = _build_repo(tmp_path)
        _install_real_script()
        client = _client(repo, tmp_path)
        task = _task(client, [_grant(client, h1)])
        plan = _write_plan(repo)

        details = _gate(repo, task, plan, tmp_path / "scratch")

        assert "touches 3 file(s)" in details


class TestNegativesFailClosed:
    def test_repo_mismatch_leaves_transport_unset(self, tmp_path: Path) -> None:
        repo, h1 = _build_repo(tmp_path)
        client = _client(repo, tmp_path)
        task = _task(client, [_grant(client, h1)])

        carry_scope_grants(task, client, repo, repo="acme/other")

        assert task.scope_drift_approved_extra_files is None
        assert task.scope_drift_approved_head is None

    def test_repo_unresolved_leaves_transport_unset(self, tmp_path: Path) -> None:
        repo, h1 = _build_repo(tmp_path)
        client = _client(repo, tmp_path)
        task = _task(client, [_grant(client, h1)])

        carry_scope_grants(task, client, repo, repo=None)

        assert task.scope_drift_approved_extra_files is None

    def test_rewritten_history_leaves_transport_unset(self, tmp_path: Path) -> None:
        repo, h1 = _build_repo(tmp_path)
        client = _client(repo, tmp_path)
        task = _task(client, [_grant(client, h1)])
        # An unrelated root commit: H1 is no longer an ancestor of HEAD, so
        # the grant must not be reused.
        git_in(repo, "checkout", "-q", "--orphan", "rewritten")
        git_in(repo, "commit", "-q", "-m", "rewrite")

        carry_scope_grants(task, client, repo, repo=_REPO_SLUG)

        assert task.scope_drift_approved_extra_files is None
        assert task.scope_drift_approved_head is None

    def test_non_impl_stage_is_a_no_op(self, tmp_path: Path) -> None:
        repo, h1 = _build_repo(tmp_path)
        client = _client(repo, tmp_path)
        task = _task(client, [_grant(client, h1)])
        task.stage = Stage.REVIEW

        carry_scope_grants(task, client, repo, repo=_REPO_SLUG)

        assert task.scope_drift_approved_extra_files is None

    def test_git_timeout_never_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, h1 = _build_repo(tmp_path)
        client = _client(repo, tmp_path)
        task = _task(client, [_grant(client, h1)])

        def _timeout(*_args: str) -> bool | None:
            raise subprocess.TimeoutExpired(cmd="git", timeout=5)

        monkeypatch.setattr(cw_git, "is_ancestor", _timeout)
        carry_scope_grants(task, client, repo, repo=_REPO_SLUG)

        assert task.scope_drift_approved_extra_files is None
