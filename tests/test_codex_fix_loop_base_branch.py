"""The codex fix loop measures against the row's base, not the trust root (#3840).

RFC 0020 S3b: ``run_review_with_fix_loop`` takes ``default_branch`` (the client
default, the trust root) and ``base_branch`` (the row's merge target). Every
merge-target carrier in the loop gets the base; ``scope_violations`` keeps the
trust root because the sensitive-file registry is read from it. A null-base row
resolves to the client default, so both names carry ``main`` and nothing
changes. The loop runs for real over a git repo whose ``origin/epic/x`` and
``origin/main`` have diverged; the spies record each call and run the real
function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from cw.codex_fix_loop import _driver, commit, review_pass, run_review_with_fix_loop
from cw.codex_review import make_codex_blocked
from cw.codex_review._engine_round import EnginePassOutcome, EngineRoundSpec
from cw.codex_runner import FakeCodexRunner
from cw.review_engine.context import EngineContext
from tests._codex_review_helpers import (
    _EditingRunner,
    _must_fix_in,
    _ok_result,
    _stem_content,
    _task,
    exclude_cw_dir,
)
from tests._shared.base_branch_builders import (
    ROW_BASE,
    TRUST_ROOT,
    diverged_base_worktree,
)
from tests._shared.git_repos import commit_tracked_file

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_CYCLE0_FILE = "pkg_a/a1.py"
_FIX_FILE = "pkg_a/fixed.py"


class _Spies:
    """Records each call to the loop's merge-target and trust-root readers."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.capture_diff: list[str] = []
        self.pre_loop: list[str] = []
        self.prepare: list[tuple[str, str | None, bool]] = []
        self.synthesize: list[str] = []
        self.scope: list[str] = []
        self.fence: list[str] = []
        self._wrap(monkeypatch, _driver, "_capture_diff", self.capture_diff, 1)
        self._wrap(monkeypatch, _driver, "pre_loop_diff_lines", self.pre_loop, 1)
        self._wrap(monkeypatch, commit, "scope_violations", self.scope, 4)
        self._spy_prepare(monkeypatch)
        self._spy_synthesize(monkeypatch)
        self._spy_fence(monkeypatch)

    @staticmethod
    def _wrap(
        monkeypatch: pytest.MonkeyPatch,
        module: object,
        name: str,
        sink: list[str],
        position: int,
    ) -> None:
        real = getattr(module, name)

        def _spy(*args: Any, **kwargs: Any) -> object:
            sink.append(args[position])
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, _spy)

    def _spy_prepare(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = review_pass._prepare_review_pass

        def _spy(*args: Any, **kwargs: Any) -> object:
            self.prepare.append(
                (
                    args[2],
                    kwargs.get("config_branch"),
                    kwargs.get("delta_from_sha") is not None,
                )
            )
            return real(*args, **kwargs)

        monkeypatch.setattr(review_pass, "_prepare_review_pass", _spy)

    def _spy_synthesize(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = review_pass.synthesize_codex_review_result

        def _spy(**kwargs: Any) -> object:
            self.synthesize.append(kwargs["default_branch"])
            return real(**kwargs)

        monkeypatch.setattr(review_pass, "synthesize_codex_review_result", _spy)

    def _spy_fence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = commit.check_fix_fence

        def _spy(*args: Any, **kwargs: Any) -> object:
            self.fence.append(kwargs["base_branch"])
            return real(*args, **kwargs)

        monkeypatch.setattr(commit, "check_fix_fence", _spy)


def _based_repo(make_git_repo: Callable[[str], Path], name: str) -> Path:
    """A feature branch cut from ``origin/<ROW_BASE>``, diverged from main."""
    repo = diverged_base_worktree(make_git_repo, name)
    for relpath in (_CYCLE0_FILE, "pkg_a/a2.py"):
        commit_tracked_file(repo, relpath, _stem_content(relpath))
    exclude_cw_dir(repo)
    return repo


def _fix(worktree: Path) -> None:
    (worktree / _FIX_FILE).write_text(_stem_content(_FIX_FILE), encoding="utf-8")


def _drive(repo: Path, *, base_branch: str) -> None:
    prepared = review_pass._prepare_review_pass(
        _task(),
        repo,
        base_branch,
        config_branch=TRUST_ROOT,
        runner=FakeCodexRunner(),
        session_id="s-base-warm",
    )
    roles = prepared.roles
    cycle_zero = [_ok_result(role) for role in roles]
    cycle_zero[0] = _ok_result(roles[0], findings=_must_fix_in(_CYCLE0_FILE))
    clean = [_ok_result(role) for role in roles]
    runner = _EditingRunner([*cycle_zero, *clean, *clean], [_fix, _fix, _fix])
    run_review_with_fix_loop(
        runner=runner,
        task=_task(),
        worktree=repo,
        default_branch=TRUST_ROOT,
        base_branch=base_branch,
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=None,
        session_id="s-base-loop",
        fix_loop_enabled=True,
    )


class TestLoopCarriers:
    @pytest.mark.parametrize(
        "base_branch", [ROW_BASE, TRUST_ROOT], ids=["base-row", "null-row"]
    )
    def test_merge_target_carriers_get_the_base_and_trust_roots_the_default(
        self,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
        base_branch: str,
    ) -> None:
        repo = _based_repo(make_git_repo, f"wt-base-{base_branch}")
        spies = _Spies(monkeypatch)

        _drive(repo, base_branch=base_branch)

        # Cycle 0's file list and the divergence baseline measure the base.
        assert spies.capture_diff == [base_branch]
        assert spies.pre_loop == [base_branch]
        # The fence's merge target is the base.
        assert spies.fence
        assert set(spies.fence) == {base_branch}
        # The sensitive-file registry stays on the trust root.
        assert spies.scope
        assert set(spies.scope) == {TRUST_ROOT}
        # The re-review diff base is the row's base; review configuration is
        # still read from the trust root, and synthesis measures the base.
        rereview = [call for call in spies.prepare if call[2]]
        assert rereview, "the loop never reached a re-review"
        assert {base for base, _config, _delta in rereview} == {base_branch}
        assert {config for _base, config, _delta in rereview} == {TRUST_ROOT}
        assert spies.synthesize
        assert set(spies.synthesize) == {base_branch}


def _engine_spec() -> EngineRoundSpec:
    return EngineRoundSpec(
        context=EngineContext(
            skill="engine skill",
            guarded_paths=(),
            ticket_id="T-1",
            client="test",
            default_branch=TRUST_ROOT,
        )
    )


def _capture_engine_inputs(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
) -> list[tuple[str, str, int]]:
    seen: list[tuple[str, str, int]] = []

    def _fake(_spec: object, inputs: Any, *, cycle: int) -> EnginePassOutcome:
        seen.append((inputs.base_branch, inputs.config_branch, cycle))
        block = make_codex_blocked(ticket_id="T-1", worktree=worktree, reason="r")
        return EnginePassOutcome(block, None, None)

    monkeypatch.setattr(review_pass, "_run_engine_pass", _fake)
    return seen


class TestEnginePath:
    def test_cycle_zero_engine_pass_gets_base_and_config_apart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _capture_engine_inputs(monkeypatch, tmp_path)

        review_pass._review_cycle_zero(
            runner=FakeCodexRunner(),
            task=_task(),
            worktree=tmp_path,
            default_branch=TRUST_ROOT,
            base_branch=ROW_BASE,
            model=None,
            reasoning_effort=None,
            wall_clock_budget_seconds=None,
            session_id="s-engine-0",
            fix_loop_enabled=True,
            claim_tier_enabled=False,
            disposition_drift_check_enabled=True,
            settled_suppression=None,
            max_prompt_chars=None,
            engine=_engine_spec(),
        )

        assert seen == [(ROW_BASE, TRUST_ROOT, 0)]

    @pytest.mark.parametrize(
        "base_branch", [ROW_BASE, TRUST_ROOT], ids=["base-row", "null-row"]
    )
    def test_rereview_engine_pass_gets_base_and_config_apart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_branch: str
    ) -> None:
        seen = _capture_engine_inputs(monkeypatch, tmp_path)

        review_pass._rereview(
            runner=FakeCodexRunner(),
            task=_task(),
            worktree=tmp_path,
            default_branch=TRUST_ROOT,
            base_branch=base_branch,
            model=None,
            reasoning_effort=None,
            remaining=None,
            session_id="s-engine-1",
            previous_reviewed_sha="0" * 40,
            prior_open_findings=[],
            cycle=1,
            engine=_engine_spec(),
        )

        assert seen == [(base_branch, TRUST_ROOT, 1)]
