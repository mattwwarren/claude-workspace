"""Tests for ``cw.review_engine.preflight``: run or park the engine round (R7)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from cw.dev_queue import add_ticket
from cw.exceptions import CwError
from cw.models import QueueItemStatus, ReviewEngineAllow, Stage
from cw.models.tasks import TicketTask
from cw.review_engine.context import EngineContext, read_engine_context
from cw.review_engine.preflight import (
    REASON_ALLOWED_HEAD,
    REASON_ENGINE_PATHS,
    REASON_NO_ENGINE_PATHS,
    PreflightResult,
    run_preflight,
)
from cw.review_engine.skill import SkillResolutionError
from tests._review_engine_helpers import (
    EngineRepo,
    commit_symlink,
    engine_context_payload,
    make_engine_repo,
    seed_row,
    write_engine_context,
)
from tests._shared.git_repos import commit_tracked_file, git_in
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

SKILL_FILE = ".claude/skills/repo-review/SKILL.md"


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> EngineRepo:
    return make_engine_repo(make_git_repo)


def _context(repo: EngineRepo) -> EngineContext:
    return read_engine_context(repo.worktree / ".claude" / "cw-context.json")


def _run(repo: EngineRepo) -> PreflightResult:
    return run_preflight(
        repo.worktree, diff_base=repo.diff_base, context=_context(repo)
    )


def _allow_row(repo: EngineRepo, head_sha: str) -> None:
    add_ticket(
        _make_ticket_task(
            ticket_id=repo.ticket,
            client=repo.client,
            stage=Stage.REVIEW,
            status=QueueItemStatus.BLOCKED_ON_USER,
            blocked_reason="review_engine_modified",
            review_engine_allows=[
                ReviewEngineAllow(
                    head_sha=head_sha,
                    actor="op",
                    reason="reviewed the skill edit",
                    recorded_at=datetime.now(UTC),
                )
            ],
        )
    )


def test_result_model_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):
        PreflightResult.model_validate(
            {"decision": "run", "reason": "r", "touched": [], "extra": 1}
        )
    with pytest.raises(ValidationError):
        PreflightResult.model_validate(
            {"decision": "maybe", "reason": "r", "touched": []}
        )


def test_decision_run_when_diff_clean(repo: EngineRepo) -> None:
    result = _run(repo)

    assert result == PreflightResult(
        decision="run", reason=REASON_NO_ENGINE_PATHS, touched=[]
    )


def test_decision_park_when_skill_file_touched(repo: EngineRepo) -> None:
    commit_tracked_file(repo.worktree, SKILL_FILE, "edited\n")

    result = _run(repo)

    assert result.decision == "park"
    assert result.reason == REASON_ENGINE_PATHS
    assert result.touched == [SKILL_FILE]


def test_decision_park_when_symlink_probe_path_touched(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(
        make_git_repo, layout=".claude/skills", symlink_to=".agents/skills"
    )
    # The branch replaces the committed symlink with a real directory that
    # carries its own copy of the skill at the probe path.
    git_in(repo.worktree, "rm", "-q", ".claude/skills")
    commit_tracked_file(
        repo.worktree, ".claude/skills/repo-review/SKILL.md", "shadow\n"
    )

    result = _run(repo)

    assert result.decision == "park"
    assert ".claude/skills/repo-review/SKILL.md" in result.touched


def test_deleting_or_retargeting_only_the_skills_symlink_is_not_flagged(
    make_git_repo: Callable[..., Path],
) -> None:
    """Characterization (see report): the link path itself is not in the guard set.

    The probe-path prefixes are ``.claude/skills/<n>`` and below, so a diff that
    only deletes the ``.claude/skills`` symlink itself reads as untouched. The
    round still materializes the default-branch copy, so the engine that runs is
    unchanged.
    """
    repo = make_engine_repo(
        make_git_repo, layout=".claude/skills", symlink_to=".agents/skills"
    )
    git_in(repo.worktree, "rm", "-q", ".claude/skills")
    git_in(repo.worktree, "commit", "-m", "drop the link")

    assert _run(repo).touched == []


def test_decision_park_when_real_path_behind_symlink_touched(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(
        make_git_repo, layout=".claude/skills", symlink_to=".agents/skills"
    )
    commit_tracked_file(
        repo.worktree, ".agents/skills/repo-review/refs/rubric.md", "weaker\n"
    )

    result = _run(repo)

    assert result.decision == "park"
    assert result.touched == [".agents/skills/repo-review/refs/rubric.md"]


def test_decision_park_when_guarded_path_touched(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo, guarded=("policy/**/*.yaml",))
    commit_tracked_file(repo.worktree, "policy/a/b.yaml", "k: v\n")
    commit_tracked_file(repo.worktree, "policy/readme.md", "x\n")

    result = _run(repo)

    assert result.decision == "park"
    assert result.touched == ["policy/a/b.yaml"]


def test_rename_away_of_skill_file_is_touched(repo: EngineRepo) -> None:
    git_in(repo.worktree, "mv", SKILL_FILE, "docs-moved.md")
    git_in(repo.worktree, "commit", "-m", "rename the skill away")

    result = _run(repo)

    assert result.decision == "park"
    assert SKILL_FILE in result.touched


def test_allowed_head_runs(repo: EngineRepo) -> None:
    commit_tracked_file(repo.worktree, SKILL_FILE, "edited\n")
    head = git_in(repo.worktree, "rev-parse", "HEAD")
    _allow_row(repo, head)

    result = _run(repo)

    assert result.decision == "run"
    assert result.reason == REASON_ALLOWED_HEAD
    assert result.touched == [SKILL_FILE]


def test_allow_for_other_head_still_parks(repo: EngineRepo) -> None:
    commit_tracked_file(repo.worktree, SKILL_FILE, "edited\n")
    _allow_row(repo, git_in(repo.worktree, "rev-parse", "HEAD"))
    commit_tracked_file(repo.worktree, "src/b.py", "y = 1\n")

    result = _run(repo)

    assert result.decision == "park"
    assert result.reason == REASON_ENGINE_PATHS


def test_row_unresolvable_fails_closed_to_park(repo: EngineRepo) -> None:
    commit_tracked_file(repo.worktree, SKILL_FILE, "edited\n")

    # No dev-queue row at all for the ticket.
    assert _run(repo).decision == "park"

    # A context naming no ticket cannot look an allow up either.
    anonymous = EngineContext(
        skill="repo-review",
        guarded_paths=(),
        ticket_id=None,
        client=None,
        default_branch="main",
    )
    result = run_preflight(repo.worktree, diff_base=repo.diff_base, context=anonymous)
    assert result.decision == "park"


def test_default_branch_unresolved_raises_cwerror(repo: EngineRepo) -> None:
    context = EngineContext(
        skill="repo-review",
        guarded_paths=(),
        ticket_id=repo.ticket,
        client=repo.client,
        default_branch="no-such-branch",
    )

    with pytest.raises(CwError, match="does not resolve"):
        run_preflight(repo.worktree, diff_base=repo.diff_base, context=context)


def test_no_engine_in_context_raises(repo: EngineRepo) -> None:
    write_engine_context(repo.worktree, engine_context_payload(skill=None))

    with pytest.raises(CwError, match=r"no review\.engine"):
        _context(repo)


def test_diff_base_not_a_commit_raises(repo: EngineRepo) -> None:
    with pytest.raises(CwError, match="git diff"):
        run_preflight(repo.worktree, diff_base="not-a-commit", context=_context(repo))


def test_head_unreadable_raises_cwerror(repo: EngineRepo, tmp_path: Path) -> None:
    commit_tracked_file(repo.worktree, SKILL_FILE, "edited\n")
    context = _context(repo)

    with pytest.raises(CwError):
        run_preflight(
            tmp_path / "not-a-repo", diff_base=repo.diff_base, context=context
        )


def test_preflight_reads_default_branch_copy_not_worktree(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo)
    # The branch swaps the skill for a command file under another layout and
    # deletes the original; the guard set still comes from the default branch.
    git_in(repo.worktree, "rm", "-rq", ".claude/skills")
    commit_tracked_file(repo.worktree, ".claude/commands/repo-review.md", "cmd\n")

    result = _run(repo)

    assert result.decision == "park"
    assert ".claude/skills/repo-review/SKILL.md" in result.touched


def test_skill_missing_on_default_branch_raises(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_engine_repo(make_git_repo, skill="repo-review")
    write_engine_context(repo.worktree, engine_context_payload(skill="other-skill"))

    with pytest.raises(SkillResolutionError):
        _run(repo)


def test_seed_row_helpers_round_trip(repo: EngineRepo) -> None:
    task = seed_row(repo, "review_engine_modified")

    assert isinstance(task, TicketTask)
    assert task.status is QueueItemStatus.BLOCKED_ON_USER
    assert task.blocked_reason == "review_engine_modified"


def test_symlink_helper_is_usable_for_probe_paths(repo: EngineRepo) -> None:
    commit_symlink(repo.worktree, "docs/link", "../README")

    assert (repo.worktree / "docs" / "link").is_symlink()
