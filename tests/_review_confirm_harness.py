"""The ``cw review confirm`` / ``delta-confirm`` test harness (#2787, #2980).

Hoisted from ``tests/test_cli_review_confirm.py`` so the resolution round's
CLI tests (``tests/test_cli_review_converge_delta.py``) drive the same seams: a
pushed feature worktree, its dev-queue row, the codex ``RealCodexRunner`` /
Claude ``RealClaudeRunner`` replaced by scripted fakes, and spies that wrap the
real ``run_review`` / ``run_claude_review``. ``Harness.argv`` names the
command, so ``args()`` builds either command's argv.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from click.testing import CliRunner

from cw import claude_review, codex_review
from cw.claude_review import FakeClaudeRunner
from cw.cli import main
from cw.codex_review import _prepare_review_pass
from cw.codex_runner import FakeCodexRunner
from cw.dev_queue import add_ticket
from cw.models import QueueItemStatus, Stage
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._codex_review_helpers import _SequencedRunner
from tests._review_convergence_helpers import warm_codex_capability
from tests.conftest import _make_ticket_task, git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    import pytest
    from click.testing import Result

    from cw.claude_review import ClaudeRunResult
    from cw.codex_runner import CodexRunResult
    from cw.models import TicketTask

#: ``cw review confirm``'s argv prefix, the default command.
CONFIRM_ARGV = ("review", "confirm")


@dataclass
class Harness:
    """A pushed feature worktree, its dev-queue row and the injected runner seam."""

    repo: Path
    task: TicketTask
    roles: list[str]
    cli: CliRunner
    monkeypatch: pytest.MonkeyPatch
    ticket: str
    client: str
    argv: tuple[str, ...] = CONFIRM_ARGV

    def results(
        self, *first: CodexRunResult, rest: CodexRunResult
    ) -> list[CodexRunResult]:
        """One result per role: *first* in role order, then *rest* for the others."""
        return [*first, *([rest] * (len(self.roles) - len(first)))]

    def all_roles(self, result: CodexRunResult) -> list[CodexRunResult]:
        return [result] * len(self.roles)

    def args(self, *extra: str) -> list[str]:
        return [
            *self.argv,
            "--ticket-id",
            self.ticket,
            "-c",
            self.client,
            "--worktree",
            str(self.repo),
            *extra,
        ]

    def invoke(
        self, results: Sequence[CodexRunResult], *extra: str
    ) -> tuple[Result, _SequencedRunner]:
        """Run the command against a ``_SequencedRunner`` queued with *results*."""
        runner = _SequencedRunner(list(results))
        self.monkeypatch.setattr("cw.codex_runner.RealCodexRunner", lambda: runner)
        return self.cli.invoke(main, self.args(*extra)), runner

    def spy_run_review(
        self, *, after: Callable[[Path], None] | None = None
    ) -> list[dict[str, object]]:
        """Wrap the real ``run_review``, recording each call's keyword arguments."""
        real = codex_review.run_review
        calls: list[dict[str, object]] = []

        def _spy(**kwargs: object) -> object:
            calls.append(dict(kwargs))
            outcome = real(**kwargs)
            if after is not None:
                after(self.repo)
            return outcome

        self.monkeypatch.setattr("cw.codex_review.run_review", _spy)
        return calls

    def head(self) -> str:
        return git_in(self.repo, "rev-parse", "HEAD")

    def patch_claude(self, results: Sequence[ClaudeRunResult]) -> FakeClaudeRunner:
        """Install a ``FakeClaudeRunner`` and a deterministic ``claude`` binary.

        ``claude`` is not in the optional-binary denylist, so the binary check
        is the ``_which_claude`` seam, patched to a deterministic path.
        """
        runner = FakeClaudeRunner(results=list(results))
        self.monkeypatch.setattr("cw.claude_review.RealClaudeRunner", lambda: runner)
        self.monkeypatch.setattr(
            "cw.claude_review._which_claude", lambda: "/usr/bin/claude"
        )
        return runner

    def invoke_claude(
        self, results: Sequence[ClaudeRunResult], *extra: str
    ) -> tuple[Result, FakeClaudeRunner]:
        """Run ``confirm --backend claude`` against a ``FakeClaudeRunner`` (#2791)."""
        runner = self.patch_claude(results)
        return self.cli.invoke(main, self.args("--backend", "claude", *extra)), runner

    def spy_claude_review(
        self, *, after: Callable[[Path], None] | None = None
    ) -> list[dict[str, object]]:
        """Wrap the real ``run_claude_review``, recording each call's kwargs."""
        real = claude_review.run_claude_review
        calls: list[dict[str, object]] = []

        def _spy(**kwargs: object) -> object:
            calls.append(dict(kwargs))
            outcome = real(**kwargs)
            if after is not None:
                after(self.repo)
            return outcome

        self.monkeypatch.setattr("cw.claude_review.run_claude_review", _spy)
        return calls


def make_harness(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    ticket: str,
    client: str,
    session: str,
    argv: tuple[str, ...] = CONFIRM_ARGV,
    client_extra: Mapping[str, object] | None = None,
) -> Harness:
    """A :class:`Harness` over a fresh pushed feature worktree.

    Writes the client (*client_extra* rides in its ``clients.yaml`` entry,
    e.g. a ``review`` block), adds the RUNNING REVIEW-stage dev-queue row,
    warms the codex capability cache and reads the reviewer roster the pass
    selects.
    """
    repo, _origin = make_git_repo_with_origin("wt-confirm")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    write_clients_yaml(
        ClientSpec(client, workspace_path=workspace, extra=dict(client_extra or {}))
    )
    task = _make_ticket_task(
        ticket_id=ticket,
        client=client,
        stage=Stage.REVIEW,
        status=QueueItemStatus.RUNNING,
        session_id=session,
        worktree_path=repo,
        scope_hint="small",
    )
    add_ticket(task)
    warm_codex_capability(repo, task)
    roles = _prepare_review_pass(
        task,
        repo,
        "main",
        config_branch="main",
        runner=FakeCodexRunner(),
        session_id="confirm-roles",
    ).roles
    return Harness(
        repo=repo,
        task=task,
        roles=list(roles),
        cli=CliRunner(),
        monkeypatch=monkeypatch,
        ticket=ticket,
        client=client,
        argv=argv,
    )
