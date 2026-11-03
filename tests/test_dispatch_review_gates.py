"""Tests for ``cw.dispatch.review_gates``: the tolerated-merge staleness pass (#2861).

The REVIEW staleness gate (#2123) parks a ticket whose sentinel's
``review.reviewed_sha`` is not the worktree's HEAD. A clean-mode ledger kept
converged across a tolerated default-branch merge ships the older clean sha,
so the gate asks the shared predicate (``tolerates_review_move``) before
parking. These tests pin that the pass is derived by cw from the worktree
ledger and git, fails closed at every step, and never applies to the codex
lane or a strict client.

Hermetic: ``current_head_sha`` is stubbed at its consumption point in
``cw.dispatch.review_gates`` and a :class:`ScriptedMergeGit` answers the
predicate's git calls (an unscripted call fails the test). The existing gate
tests stay in ``tests/test_dispatch.py`` (``TestReviewStalenessGate``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.dev_queue import REVIEW_STALENESS_GATE_DISPOSITION
from cw.dispatch import review_gates
from cw.dispatch.review_gates import (
    _REVIEW_STALENESS_REASON,
    _should_gate_for_review_staleness,
)
from cw.models import QueueItemStatus, Stage
from cw.review_convergence import LEDGER_RELATIVE_PATH
from tests._review_convergence_helpers import (
    GitScript,
    ScriptedMergeGit,
    branch_touching_merge_script,
    confirmed_ledger,
    make_review_client,
    non_merge_commit_script,
    tolerated_merge_script,
    write_ledger,
)
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask
    from cw.review_convergence import ConvergenceLedger

CLEAN = "c" * 40
HEAD = "d" * 40
DEFAULT = "e" * 40
_CLIENT = "test-client"


def _stub_head(monkeypatch: pytest.MonkeyPatch, sha: str | None) -> None:
    """Stub the HEAD probe where the gate reads it (``review_gates``' binding)."""
    monkeypatch.setattr(review_gates, "current_head_sha", lambda _path: sha)


def _task(worktree: Path | None) -> TicketTask:
    return _make_ticket_task(
        status=QueueItemStatus.RUNNING,
        stage=Stage.REVIEW,
        worktree_path=worktree,
        client=_CLIENT,
    )


def _clients(
    worktree: Path, *, convergence: str = "clean", default_merge_tolerance: bool = True
) -> dict[str, ClientConfig]:
    return {
        _CLIENT: make_review_client(
            worktree,
            convergence=convergence,
            default_merge_tolerance=default_merge_tolerance,
        )
    }


def _sentinel(reviewed_sha: str | None = CLEAN) -> dict[str, object]:
    review: dict[str, object] = {"agents_run": 3}
    if reviewed_sha is not None:
        review["reviewed_sha"] = reviewed_sha
    return {"review": review}


def _gates(
    worktree: Path,
    *,
    ledger: ConvergenceLedger | None = None,
    script: GitScript | None = None,
    monkeypatch: pytest.MonkeyPatch,
    convergence: str = "clean",
    default_merge_tolerance: bool = True,
) -> tuple[bool, ScriptedMergeGit]:
    """Run the gate on a REVIEW task at *worktree* with HEAD stubbed to ``HEAD``."""
    if ledger is not None:
        write_ledger(worktree, ledger)
    fake = ScriptedMergeGit(script or {}).install(monkeypatch)
    _stub_head(monkeypatch, HEAD)
    gated = _should_gate_for_review_staleness(
        _task(worktree),
        _sentinel(),
        _clients(
            worktree,
            convergence=convergence,
            default_merge_tolerance=default_merge_tolerance,
        ),
    )
    return gated, fake


class TestReviewStalenessToleratedMerge:
    def test_a_tolerated_merge_after_the_clean_round_does_not_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gated, fake = _gates(
            tmp_path,
            ledger=confirmed_ledger(sha=CLEAN),
            script=tolerated_merge_script(CLEAN, HEAD, DEFAULT),
            monkeypatch=monkeypatch,
        )
        assert gated is False
        assert fake.calls != []

    @pytest.mark.parametrize(
        "build",
        [
            pytest.param(non_merge_commit_script, id="non-merge-commit"),
            pytest.param(branch_touching_merge_script, id="branch-touching-merge"),
        ],
    )
    def test_a_move_that_is_not_tolerated_still_gates(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        build: Callable[[str, str, str], GitScript],
    ) -> None:
        gated, _ = _gates(
            tmp_path,
            ledger=confirmed_ledger(sha=CLEAN),
            script=build(CLEAN, HEAD, DEFAULT),
            monkeypatch=monkeypatch,
        )
        assert gated is True

    def test_a_strict_client_gates_without_a_git_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gated, fake = _gates(
            tmp_path,
            ledger=confirmed_ledger(sha=CLEAN),
            script=tolerated_merge_script(CLEAN, HEAD, DEFAULT),
            monkeypatch=monkeypatch,
            default_merge_tolerance=False,
        )
        assert gated is True
        assert fake.calls == []

    def test_a_blocking_client_gates_without_a_git_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gated, fake = _gates(
            tmp_path,
            ledger=confirmed_ledger(sha=CLEAN),
            script=tolerated_merge_script(CLEAN, HEAD, DEFAULT),
            monkeypatch=monkeypatch,
            convergence="blocking",
        )
        assert gated is True
        assert fake.calls == []

    def test_a_codex_ledger_gates_without_a_git_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex-lane pin: ``final_round_clean_sha`` never trails ``reviewed_sha``."""
        codex = confirmed_ledger(sha=CLEAN).model_copy(update={"loop_backend": "codex"})
        gated, fake = _gates(
            tmp_path,
            ledger=codex,
            script=tolerated_merge_script(CLEAN, HEAD, DEFAULT),
            monkeypatch=monkeypatch,
        )
        assert gated is True
        assert fake.calls == []

    @pytest.mark.parametrize("answer", [128, OSError("git vanished")])
    def test_unreadable_git_state_gates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: int | OSError
    ) -> None:
        script = tolerated_merge_script(CLEAN, HEAD, DEFAULT)
        script["rev-parse", "refs/remotes/origin/main^{commit}"] = answer
        gated, _ = _gates(
            tmp_path,
            ledger=confirmed_ledger(sha=CLEAN),
            script=script,
            monkeypatch=monkeypatch,
        )
        assert gated is True

    def test_a_missing_ledger_gates_without_a_git_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gated, fake = _gates(tmp_path, monkeypatch=monkeypatch)
        assert gated is True
        assert fake.calls == []

    def test_a_corrupt_ledger_gates_without_a_git_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / LEDGER_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xfe")
        gated, fake = _gates(tmp_path, monkeypatch=monkeypatch)
        assert gated is True
        assert fake.calls == []

    def test_a_missing_reviewed_sha_gates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_ledger(tmp_path, confirmed_ledger(sha=CLEAN))
        fake = ScriptedMergeGit({}).install(monkeypatch)
        _stub_head(monkeypatch, HEAD)
        gated = _should_gate_for_review_staleness(
            _task(tmp_path), _sentinel(None), _clients(tmp_path)
        )
        assert gated is True
        assert fake.calls == []

    def test_a_missing_worktree_gates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No stamped path and no checkout at the derived path: unresolvable."""
        fake = ScriptedMergeGit({}).install(monkeypatch)
        _stub_head(monkeypatch, HEAD)
        gated = _should_gate_for_review_staleness(
            _task(None), _sentinel(), _clients(tmp_path / "absent")
        )
        assert gated is True
        assert fake.calls == []

    def test_an_unmeasurable_head_gates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_ledger(tmp_path, confirmed_ledger(sha=CLEAN))
        fake = ScriptedMergeGit({}).install(monkeypatch)
        _stub_head(monkeypatch, None)
        gated = _should_gate_for_review_staleness(
            _task(tmp_path), _sentinel(), _clients(tmp_path)
        )
        assert gated is True
        assert fake.calls == []

    def test_a_review_at_head_does_not_gate_without_a_git_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = ScriptedMergeGit({}).install(monkeypatch)
        _stub_head(monkeypatch, HEAD)
        gated = _should_gate_for_review_staleness(
            _task(tmp_path), _sentinel(HEAD), _clients(tmp_path)
        )
        assert gated is False
        assert fake.calls == []

    def test_the_stale_review_tokens_are_unchanged(self) -> None:
        assert REVIEW_STALENESS_GATE_DISPOSITION == "review_artifacts_stale"
        assert _REVIEW_STALENESS_REASON == "review_artifacts_stale"
