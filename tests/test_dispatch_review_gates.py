"""Tests for ``cw.dispatch.review_gates``: the tolerated-merge staleness pass (#2861).

The REVIEW staleness gate (#2123) parks a ticket whose sentinel's
``review.reviewed_sha`` is not the worktree's HEAD. A clean-mode ledger kept
converged across a tolerated default-branch merge ships the older clean sha,
so the gate asks the shared predicate (``tolerates_review_move``) before
parking. These tests pin that the pass is derived by cw from the worktree
ledger and git, fails closed at every step, never applies to a strict client,
and since #2979 tolerates any merge git itself would write, on every loop
backend, whatever files it touches.

Hermetic: ``TestReviewStalenessToleratedMerge`` stubs ``current_head_sha`` at
its consumption point in ``cw.dispatch.review_gates`` and lets a
:class:`ScriptedMergeGit` answer the predicate's git calls (an unscripted call
fails the test). Those fakes ignore their path argument, so
``TestReviewStalenessRealRepo`` runs the gate against a real repo, a real
ledger and a real client config with nothing stubbed. The existing gate tests
stay in ``tests/test_dispatch.py`` (``TestReviewStalenessGate``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from cw.dev_queue import REVIEW_STALENESS_GATE_DISPOSITION
from cw.dispatch import review_gates
from cw.dispatch.review_gates import (
    _REVIEW_STALENESS_REASON,
    _should_gate_for_review_staleness,
    plan_review_staleness,
)
from cw.dispatch.review_tip_verdicts import (
    TipCheck,
    TipKey,
    precomputed_tip_verdicts,
)
from cw.models import QueueItemStatus, Stage
from cw.review_convergence import LEDGER_RELATIVE_PATH, tolerates_review_move
from tests._review_convergence_helpers import (
    MAIN_ONLY_FILE,
    GitScript,
    ScriptedMergeGit,
    branch_that_merged_main,
    branch_with_conflict_resolution_merge,
    branch_with_overlap_merge,
    confirmed_ledger,
    make_review_client,
    merge_origin_commit,
    merge_origin_over_branch_file,
    merge_tree_key,
    non_merge_commit_script,
    tolerated_merge_script,
    write_ledger,
)
from tests._shared.dispatch_fixtures import stub_head as _stub_head
from tests._shared.git_repos import git_in
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask
    from cw.review_convergence import ConvergenceLedger

CLEAN = "c" * 40
HEAD = "d" * 40
DEFAULT = "e" * 40
_CLIENT = "test-client"


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


def _conflict_merge_script(clean: str, head: str, default_sha: str) -> GitScript:
    """:func:`tolerated_merge_script` whose merge ``merge-tree`` calls a conflict."""
    script = tolerated_merge_script(clean, head, default_sha)
    script[merge_tree_key(head)] = 1
    return script


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
            pytest.param(_conflict_merge_script, id="conflict-resolution-merge"),
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

    def test_a_codex_ledger_across_a_tolerated_merge_does_not_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2979: the claude-native allow-list is gone; the rule is backend-free."""
        codex = confirmed_ledger(sha=CLEAN, loop_backend="codex")
        gated, fake = _gates(
            tmp_path,
            ledger=codex,
            script=tolerated_merge_script(CLEAN, HEAD, DEFAULT),
            monkeypatch=monkeypatch,
        )
        assert gated is False
        assert fake.calls != []

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

    def test_a_nul_sha_gates_instead_of_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker-writable sha with a NUL makes ``subprocess.run`` raise.

        The gate's contract is that every unreadable input parks, so the
        predicate's git wrapper must absorb it rather than let it escape.
        """
        nul_sha = "a\x00b"
        write_ledger(tmp_path, confirmed_ledger(sha=nul_sha))
        scripted = ScriptedMergeGit(tolerated_merge_script(nul_sha, HEAD, DEFAULT))

        def run_git_like_subprocess(
            argv: Sequence[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if any("\x00" in arg for arg in argv):
                msg = "embedded null byte"
                raise ValueError(msg)
            return scripted.run_git(argv, **kwargs)

        scripted.install(monkeypatch)
        monkeypatch.setattr(
            "cw.review_convergence._merge_tolerance.run_git", run_git_like_subprocess
        )
        _stub_head(monkeypatch, HEAD)
        gated = _should_gate_for_review_staleness(
            _task(tmp_path), _sentinel(nul_sha), _clients(tmp_path)
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


class TestReviewStalenessRealRepo:
    """The gate against a real repo, ledger and client config (#2861).

    Nothing is stubbed: ``current_head_sha``, the worktree resolution and every
    git call of the predicate run for real, so a wrong worktree handed to the
    predicate or a client field that does not name the real ``origin/<default>``
    turns these red where the path-ignoring fakes above stay green.
    """

    @staticmethod
    def _converged_branch(
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    ) -> tuple[Path, str]:
        """A feature branch with a converged claude-native ledger at its tip."""
        repo, _origin = make_git_repo_with_origin("real-gate")
        clean = git_in(repo, "rev-parse", "HEAD")
        write_ledger(repo, confirmed_ledger(sha=clean))
        return repo, clean

    @staticmethod
    def _gated(repo: Path, clean: str, *, default_merge_tolerance: bool = True) -> bool:
        return _should_gate_for_review_staleness(
            _task(repo),
            _sentinel(clean),
            _clients(repo, default_merge_tolerance=default_merge_tolerance),
        )

    @pytest.mark.parametrize("backend", ["claude-native", "codex"])
    def test_an_overlapping_clean_merge_does_not_gate(
        self,
        tmp_path: Path,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
        backend: str,
    ) -> None:
        """#2979: overlap with the branch's own file no longer matters."""
        repo, _origin = make_git_repo_with_origin("real-gate")
        found = branch_with_overlap_merge(repo, tmp_path / "scratch")
        write_ledger(repo, confirmed_ledger(sha=found.clean, loop_backend=backend))

        assert self._gated(repo, found.clean) is False

    def test_a_conflict_resolution_merge_gates(
        self,
        tmp_path: Path,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    ) -> None:
        repo, _origin = make_git_repo_with_origin("real-gate")
        found = branch_with_conflict_resolution_merge(repo, tmp_path / "scratch")
        write_ledger(repo, confirmed_ledger(sha=found.clean))

        assert self._gated(repo, found.clean) is True

    def test_the_strict_opt_out_gates_a_clean_merge(
        self,
        tmp_path: Path,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    ) -> None:
        repo, _origin = make_git_repo_with_origin("real-gate")
        found = branch_with_overlap_merge(repo, tmp_path / "scratch")
        write_ledger(repo, confirmed_ledger(sha=found.clean))

        assert self._gated(repo, found.clean, default_merge_tolerance=False) is True

    def test_a_tolerated_merge_does_not_gate(
        self,
        tmp_path: Path,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    ) -> None:
        repo, clean = self._converged_branch(make_git_repo_with_origin)
        merge_origin_commit(repo, tmp_path / "main-clone", "main", MAIN_ONLY_FILE)
        assert git_in(repo, "rev-parse", "HEAD") != clean

        assert self._gated(repo, clean) is False

    def test_a_non_merge_commit_after_the_merge_gates(
        self,
        tmp_path: Path,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    ) -> None:
        repo, clean = self._converged_branch(make_git_repo_with_origin)
        branch_that_merged_main(repo, tmp_path / "main-clone")

        assert self._gated(repo, clean) is True

    def test_an_add_add_conflict_resolved_to_main_gates(
        self,
        tmp_path: Path,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    ) -> None:
        repo, clean = self._converged_branch(make_git_repo_with_origin)
        merge_origin_over_branch_file(
            repo, tmp_path / "main-clone", "main", "feature.py"
        )

        assert self._gated(repo, clean) is True


class TestReviewStalenessInstalledVerdict:
    """The locked gate reads the pre-lock verdict, never a stale one (#2994)."""

    @staticmethod
    def _key(task: TicketTask, *, reviewed: str = CLEAN, head: str = HEAD) -> TipKey:
        return TipKey(_CLIENT, task.ticket_id, reviewed, head)

    @staticmethod
    def _inline(monkeypatch: pytest.MonkeyPatch, answer: bool) -> list[tuple[str, str]]:
        """Patch the inline predicate; returns the ``(reviewed, head)`` it saw."""
        calls: list[tuple[str, str]] = []

        def fake(
            _client: object,
            _worktree: object,
            *,
            reviewed_sha: str,
            head_sha: str,
            task: object,
        ) -> bool:
            calls.append((reviewed_sha, head_sha))
            return answer

        monkeypatch.setattr(review_gates, "tolerates_review_move", fake)
        return calls

    @staticmethod
    def _gate(worktree: Path, task: TicketTask) -> bool:
        return _should_gate_for_review_staleness(task, _sentinel(), _clients(worktree))

    @staticmethod
    def _converge(worktree: Path) -> None:
        write_ledger(worktree, confirmed_ledger(sha=CLEAN))

    def test_an_installed_true_verdict_does_not_gate_and_runs_no_tip_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._converge(tmp_path)
        _stub_head(monkeypatch, HEAD)
        fake = ScriptedMergeGit({}).install(monkeypatch)
        inline = self._inline(monkeypatch, answer=False)
        task = _task(tmp_path)

        with precomputed_tip_verdicts({self._key(task): True}):
            gated = self._gate(tmp_path, task)

        assert gated is False
        assert inline == []
        assert fake.calls == []

    def test_an_installed_false_verdict_gates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._converge(tmp_path)
        _stub_head(monkeypatch, HEAD)
        inline = self._inline(monkeypatch, answer=True)
        task = _task(tmp_path)

        with precomputed_tip_verdicts({self._key(task): False}):
            gated = self._gate(tmp_path, task)

        assert gated is True
        assert inline == []

    @pytest.mark.parametrize("installed", [True, False])
    @pytest.mark.parametrize("field", ["head", "reviewed"])
    def test_a_verdict_for_another_head_or_reviewed_sha_is_not_applied(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        field: str,
        installed: bool,
    ) -> None:
        """The race: HEAD or the ledger moved after the sweep, so the key differs.

        The installed answer is the opposite of the inline one, in both
        polarities, so the gate provably followed the inline check.
        """
        self._converge(tmp_path)
        _stub_head(monkeypatch, HEAD)
        inline = self._inline(monkeypatch, answer=not installed)
        task = _task(tmp_path)
        other = "f" * 40
        stale = (
            self._key(task, head=other)
            if field == "head"
            else self._key(task, reviewed=other)
        )

        with (
            caplog.at_level(logging.WARNING, logger="cw.dispatch"),
            precomputed_tip_verdicts({stale: installed}),
        ):
            gated = self._gate(tmp_path, task)

        assert inline == [(CLEAN, HEAD)]
        assert gated is installed
        assert caplog.text.count("no precomputed verdict") == 1

    def test_no_scope_runs_the_tip_check_inline_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._converge(tmp_path)
        _stub_head(monkeypatch, HEAD)
        inline = self._inline(monkeypatch, answer=True)

        gated = self._gate(tmp_path, _task(tmp_path))

        assert gated is False
        assert inline == [(CLEAN, HEAD)]

    def test_the_gate_emits_no_log_without_a_scope(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self._converge(tmp_path)
        _stub_head(monkeypatch, HEAD)
        self._inline(monkeypatch, answer=True)

        with caplog.at_level(logging.DEBUG, logger="cw.dispatch"):
            self._gate(tmp_path, _task(tmp_path))

        assert caplog.records == []

    def test_plan_is_a_bool_when_unresolvable_and_a_tipcheck_only_when_git_is_needed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task = _task(tmp_path)
        clients = _clients(tmp_path)

        # No review block at all: the gate is out of scope.
        assert plan_review_staleness(task, {}, clients) is False
        assert plan_review_staleness(task, None, clients) is False
        # A review block that names no reviewed sha fails closed.
        no_sha = {"review": {"agents_run": 3}}
        assert plan_review_staleness(task, no_sha, clients) is True
        # An unresolvable worktree fails closed.
        assert plan_review_staleness(_task(None), _sentinel(), {}) is True
        # An unmeasurable HEAD fails closed.
        _stub_head(monkeypatch, None)
        assert plan_review_staleness(task, _sentinel(), clients) is True
        # The reviewed sha is HEAD: fresh, no git.
        _stub_head(monkeypatch, CLEAN)
        assert plan_review_staleness(task, _sentinel(), clients) is False
        # HEAD moved but no ledger is converged on the reviewed sha: stale, no git.
        _stub_head(monkeypatch, HEAD)
        assert plan_review_staleness(task, _sentinel(), clients) is True
        # HEAD moved and the ledger qualifies: only the tip check is left.
        self._converge(tmp_path)
        plan = plan_review_staleness(task, _sentinel(), clients)
        assert isinstance(plan, TipCheck)
        assert plan.key == self._key(task)
        assert plan.worktree == tmp_path
        assert plan.client is clients[_CLIENT]

    @pytest.mark.parametrize(
        ("convergence", "default_merge_tolerance", "ledger", "with_client"),
        [
            pytest.param("clean", False, True, True, id="strict-opt-out"),
            pytest.param("blocking", True, True, True, id="blocking-mode"),
            pytest.param("clean", True, False, True, id="no-ledger"),
            pytest.param("clean", True, True, False, id="no-client"),
        ],
    )
    def test_plan_and_tolerates_review_move_agree(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        convergence: str,
        default_merge_tolerance: bool,
        ledger: bool,
        with_client: bool,
    ) -> None:
        """A plan that is a final ``True`` means the predicate is False with no git.

        Pins ``review_move_tolerance``, the plan's git-free precheck, to
        ``tolerates_review_move`` itself: if they disagreed, the sweep would
        skip a check the gate needs (or run one it does not).
        """
        if ledger:
            write_ledger(tmp_path, confirmed_ledger(sha=CLEAN))
        _stub_head(monkeypatch, HEAD)
        client = make_review_client(
            tmp_path,
            convergence=convergence,
            default_merge_tolerance=default_merge_tolerance,
            name=_CLIENT,
        )
        clients = {_CLIENT: client} if with_client else {}
        task = _task(tmp_path)

        assert plan_review_staleness(task, _sentinel(), clients) is True

        fake = ScriptedMergeGit({}).install(monkeypatch)
        assert (
            tolerates_review_move(
                clients.get(_CLIENT), tmp_path, reviewed_sha=CLEAN, head_sha=HEAD
            )
            is False
        )
        assert fake.calls == []
