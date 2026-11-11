"""The codex REVIEW result's stage facts, stamped once at the door (#3056).

``plan_source``, ``fork_point_sha`` and the fix-loop ``commits`` are written
onto the result between the review and ``complete_session_through_door``, so
FINALIZE reads real values instead of the ``"none"`` / ``None`` / ``[]`` the
result builders hardcode.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from cw import context_queue_metadata
from cw._git import run_git
from cw.auto_dev_result import AutoDevResult, parse_stdout
from cw.codex_review import FIX_CYCLE_COMMIT_PREFIX, make_codex_blocked
from cw.codex_stage_facts import fix_loop_facts, resolve_plan_source, stamp_stage_facts
from cw.dispatch.routing import scope_tier
from tests._codex_review_helpers import _task
from tests._shared.git_repos import add_bare_origin, git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

_SHA_LENGTH = 40
_PLAN_NONE_FRICTION = "codex_plan_source_none"


def _commit(repo: Path, name: str, subject: str) -> str:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    git_in(repo, "add", name)
    git_in(repo, "commit", "-m", subject)
    return git_in(repo, "rev-parse", "HEAD")


def _fix_subject(cycle: int) -> str:
    return f"{FIX_CYCLE_COMMIT_PREFIX} {cycle} — tidy"


def _plan(repo: Path) -> None:
    (repo / ".cw").mkdir(exist_ok=True)
    (repo / ".cw" / "plan.md").write_text("# plan\n", encoding="utf-8")


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> Path:
    path = make_git_repo("facts-wt")
    add_bare_origin(path)
    git_in(path, "checkout", "-b", "feature")
    return path


def _result(repo: Path) -> AutoDevResult:
    return make_codex_blocked(ticket_id="T-1", worktree=repo, reason="r")


class TestPlanSource:
    def test_valid_value_with_a_plan_file(self, repo: Path) -> None:
        _plan(repo)
        task = _task().model_copy(update={"plan_source": "generated"})

        assert resolve_plan_source(task, repo) == ("generated", None)

    @pytest.mark.parametrize(
        ("value", "with_plan", "why"),
        [
            (None, True, "unset"),
            ("bogus", True, "not in the closed PlanSource set"),
            ("generated", False, "absent from the worktree"),
        ],
    )
    def test_anything_else_is_none_with_a_friction_token(
        self, repo: Path, value: str | None, with_plan: bool, why: str
    ) -> None:
        if with_plan:
            _plan(repo)
        task = _task().model_copy(update={"plan_source": value})

        source, token = resolve_plan_source(task, repo)

        assert source == "none"
        assert token is not None
        assert token.startswith(f"{_PLAN_NONE_FRICTION}: ")
        assert why in token


class TestFixLoopFacts:
    def test_commits_are_exactly_the_fix_commits_newest_first(self, repo: Path) -> None:
        _commit(repo, "impl1.py", "feat: first")
        old_fix = _commit(repo, "fix1.py", _fix_subject(1))
        _commit(repo, "between.py", "feat: between fixes")
        new_fix = _commit(repo, "fix2.py", _fix_subject(2))

        fork, commits = fix_loop_facts(repo, "main")

        assert commits == [new_fix, old_fix]
        assert all(len(sha) == _SHA_LENGTH for sha in commits)
        assert fork == git_in(repo, "merge-base", "origin/main", "HEAD")

    def test_fork_point_is_not_the_pre_loop_head(self, repo: Path) -> None:
        pre_loop = _commit(repo, "impl.py", "feat: impl")
        _commit(repo, "fix.py", _fix_subject(1))

        fork, _commits = fix_loop_facts(repo, "main")

        assert fork != pre_loop
        assert fork == git_in(repo, "rev-parse", "origin/main")

    def test_no_origin_gives_none_and_empty(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        bare = make_git_repo("no-origin")
        _commit(bare, "fix.py", _fix_subject(1))

        assert fix_loop_facts(bare, "main") == (None, [])

    def test_unresolvable_log_keeps_the_fork(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _commit(repo, "fix.py", _fix_subject(1))
        real = run_git

        def _no_log(
            argv: Sequence[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            if argv[0] == "log":
                return subprocess.CompletedProcess(argv, 128, "", "bad revision")
            return real(argv, **kwargs)

        monkeypatch.setattr("cw.codex_stage_facts.run_git", _no_log)

        with caplog.at_level(logging.WARNING, logger="cw.codex_stage_facts"):
            fork, commits = fix_loop_facts(repo, "main")

        assert fork is not None
        assert commits == []
        assert any(
            "codex_stage_facts_git_log_failed" in r.getMessage() for r in caplog.records
        )


class TestStamp:
    def test_stamps_all_three_fields_and_a_note(self, repo: Path) -> None:
        _plan(repo)
        task = _task().model_copy(update={"plan_source": "generated"})
        fix = _commit(repo, "fix.py", _fix_subject(1))

        stamped = stamp_stage_facts(
            _result(repo), task=task, worktree=repo, default_branch="main"
        )

        assert stamped.plan_source == "generated"
        assert stamped.fork_point_sha == git_in(repo, "rev-parse", "origin/main")
        assert stamped.commits == [fix]
        assert not any(_PLAN_NONE_FRICTION in t for t in stamped.friction_highlights)

    def test_a_missing_plan_adds_the_friction_token(self, repo: Path) -> None:
        stamped = stamp_stage_facts(
            _result(repo), task=_task(), worktree=repo, default_branch="main"
        )

        assert stamped.plan_source == "none"
        assert [
            t for t in stamped.friction_highlights if t.startswith(_PLAN_NONE_FRICTION)
        ]

    def test_empty_diff_blocked_comes_back_unchanged(self, repo: Path) -> None:
        result = _result(repo).model_copy(update={"status": "empty_diff_blocked"})

        stamped = stamp_stage_facts(
            result, task=_task(), worktree=repo, default_branch="main"
        )

        assert stamped is result

    def test_no_origin_leaves_fork_and_commits_empty(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        bare = make_git_repo("stamp-no-origin")

        stamped = stamp_stage_facts(
            _result(bare), task=_task(), worktree=bare, default_branch="main"
        )

        assert stamped.fork_point_sha is None
        assert stamped.commits == []


class TestFinalizeAcceptance:
    def test_a_stamped_plan_source_reaches_the_finalize_queue_metadata(
        self, repo: Path
    ) -> None:
        _plan(repo)
        task = _task().model_copy(update={"plan_source": "generated"})
        stamped = stamp_stage_facts(
            _result(repo), task=task, worktree=repo, default_branch="main"
        )
        payload = stamped.model_dump(mode="json")

        framed = parse_stdout(
            "<<<AUTO_DEV_RESULT\n" + json.dumps(payload) + "\nAUTO_DEV_RESULT>>>"
        )
        assert isinstance(framed, AutoDevResult)
        assert isinstance(AutoDevResult.model_validate(payload), AutoDevResult)

        finalize_task = _task()
        assert finalize_task.plan_source is None
        scope_tier._persist_carried_context(finalize_task, payload)
        metadata = context_queue_metadata.build_queue_metadata(finalize_task)

        assert metadata["plan_source"] == "generated"
