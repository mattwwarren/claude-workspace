"""Tests for cw.stage_verification.impl_staleness: the staleness check (#3529).

The reason and filter tests drive the repo's real ``check_impl_guard_staleness.py``
(installed under the per-test HOME); the exit-code dispositions use stubs.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from cw.models import Stage, TicketTask
from cw.models.orchestrator_config.constants import HOOK_CONTEXT_RELATIVE_PATH
from cw.stage_verification.impl_block import IMPL_FAILED
from cw.stage_verification.impl_staleness import (
    AGENT_ONLY_FRICTION,
    CHECK_FAILED_FRICTION,
    COMMENTS_LOAD_FAILED_FRICTION,
    SCRIPT_ABSENT_FRICTION,
    SCRIPT_NAME,
    Staleness,
    check_staleness,
    operator_comments_after,
    read_regressed_into_stage,
    regress_signal,
)
from tests._shared.git_repos import commit_tracked_file, git_in
from tests._shared.scope_grant_builders import install_real_guard_script
from tests._shared.stop_hook_context import _write_hook_context_file
from tests._shared.ticket_context import make_comment

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cw.ticket_snapshot import SnapshotComment

_LONG_AGO = "2000-01-01T00:00:00Z"
_FAR_FUTURE = "2099-01-01T00:00:00Z"
_PRINT_VERDICT = "import json, sys\nprint(json.dumps({verdict}))\n"


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> Path:
    worktree = make_git_repo("staleness-wt")
    commit_tracked_file(worktree, "a.py")
    return worktree


def _stub(body: str, *, version: int = 2) -> Path:
    script = Path.home() / ".claude" / "scripts" / SCRIPT_NAME
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"# cw-script-version: {version}\n{body}", encoding="utf-8")
    return script


def _check(
    repo: Path,
    tmp_path: Path,
    *,
    comments: Sequence[SnapshotComment] = (),
    signal: str = "",
) -> Staleness:
    return check_staleness(
        repo, comments=comments, signal=signal, scratch=tmp_path / "scratch"
    )


def _task(**updates: object) -> TicketTask:
    return TicketTask.model_validate(
        {"ticket_id": "T-1", "client": "test", "stage": Stage.IMPL, **updates}
    )


class TestCheckStalenessDispositions:
    def test_an_absent_script_is_friction_and_not_stale(
        self, repo: Path, tmp_path: Path
    ) -> None:
        result = _check(repo, tmp_path)
        assert result.stale is False
        assert result.block is None
        assert result.friction == (SCRIPT_ABSENT_FRICTION,)

    def test_an_absent_script_with_a_regress_signal_is_stale(
        self, repo: Path, tmp_path: Path
    ) -> None:
        result = _check(repo, tmp_path, signal="impl")
        assert result.stale is True
        assert result.block is None
        assert result.reasons == ("regressed_to_impl",)
        assert result.friction == (SCRIPT_ABSENT_FRICTION,)

    def test_exit_2_with_a_regress_signal_is_stale(
        self, repo: Path, tmp_path: Path
    ) -> None:
        _stub("import sys\nsys.exit(2)\n")
        result = _check(repo, tmp_path, signal="impl")
        assert result.stale is True
        assert result.block is None
        assert result.reasons == ("regressed_to_impl",)
        assert result.friction == (CHECK_FAILED_FRICTION,)

    def test_a_marker_below_the_minimum_is_the_headless_block(
        self, repo: Path, tmp_path: Path
    ) -> None:
        script = _stub("", version=1)
        result = _check(repo, tmp_path)
        assert result.block is not None
        assert result.block.reason == IMPL_FAILED
        assert result.block.details == (
            "Pre-Stage Detector Guard: HEADLESS BLOCK — "
            f"check_impl_guard_staleness.py at {script} — missing/stale "
            "cw-script-version marker (need >= 2)"
        )

    def test_exit_2_is_friction_and_fails_open(
        self, repo: Path, tmp_path: Path
    ) -> None:
        _stub("import sys\nsys.exit(2)\n")
        result = _check(repo, tmp_path)
        assert result.stale is False
        assert result.block is None
        assert result.friction == (CHECK_FAILED_FRICTION,)

    def test_any_other_exit_blocks(self, repo: Path, tmp_path: Path) -> None:
        _stub("import sys\nprint('boom', file=sys.stderr)\nsys.exit(5)\n")
        result = _check(repo, tmp_path)
        assert result.block is not None
        assert result.block.reason == IMPL_FAILED
        assert "exit 5" in result.block.details
        assert "boom" in result.block.details

    def test_non_json_output_blocks(self, repo: Path, tmp_path: Path) -> None:
        _stub("print('not json')\n")
        result = _check(repo, tmp_path)
        assert result.block is not None
        assert "without a valid verdict" in result.block.details

    def test_a_non_object_verdict_blocks(self, repo: Path, tmp_path: Path) -> None:
        _stub("print('[1]')\n")
        result = _check(repo, tmp_path)
        assert result.block is not None

    def test_a_script_that_does_not_run_blocks(
        self, repo: Path, tmp_path: Path
    ) -> None:
        _stub("")
        real_run = subprocess.run

        def _run(argv: list[str], *args: Any, **kwargs: Any) -> Any:
            if argv[0] == sys.executable:
                message = "no exec"
                raise OSError(message)
            return real_run(argv, *args, **kwargs)

        with patch("cw.stage_verification.impl_staleness.subprocess.run", _run):
            result = _check(repo, tmp_path)
        assert result.block is not None
        assert "did not run" in result.block.details
        assert "no exec" in result.block.details

    def test_an_unreadable_head_date_blocks(self, repo: Path, tmp_path: Path) -> None:
        _stub("")
        with patch(
            "cw.stage_verification.impl_staleness.git_output",
            side_effect=subprocess.CalledProcessError(128, "log"),
        ):
            result = _check(repo, tmp_path)
        assert result.block is not None
        assert "commit date" in result.block.details

    def test_comments_load_failed_is_friction(self, repo: Path, tmp_path: Path) -> None:
        verdict = {"reasons": [], "comments_load_failed": True}
        _stub(_PRINT_VERDICT.format(verdict=verdict))
        result = _check(repo, tmp_path)
        assert result.stale is False
        assert result.friction == (COMMENTS_LOAD_FAILED_FRICTION,)

    def test_a_non_list_reasons_value_is_not_stale(
        self, repo: Path, tmp_path: Path
    ) -> None:
        _stub('print(\'{"reasons": "x"}\')\n')
        result = _check(repo, tmp_path)
        assert result.stale is False
        assert result.reasons == ()


class TestCheckStalenessReasons:
    """The real script's verdict, then cw's provenance filter."""

    @pytest.fixture(autouse=True)
    def _real_script(self) -> None:
        install_real_guard_script("check_impl_guard_staleness.py")

    def test_nothing_newer_and_no_signal_is_not_stale(
        self, repo: Path, tmp_path: Path
    ) -> None:
        old = make_comment("c1", created_at=_LONG_AGO)
        result = _check(repo, tmp_path, comments=[old])
        assert result.stale is False
        assert result.reasons == ()
        assert result.friction == ()

    def test_a_regress_signal_is_stale(self, repo: Path, tmp_path: Path) -> None:
        result = _check(repo, tmp_path, signal="impl")
        assert result.stale is True
        assert result.reasons == ("regressed_to_impl",)

    def test_an_operator_comment_after_head_is_stale_and_delivered(
        self, repo: Path, tmp_path: Path
    ) -> None:
        newer = make_comment("c1", body="do X too", created_at=_FAR_FUTURE)
        result = _check(repo, tmp_path, comments=[newer])
        assert result.stale is True
        assert result.reasons == ("stale_comment_after_head",)
        assert result.operator_comments == (newer,)

    def test_the_comments_file_carries_created_at_and_body(
        self, repo: Path, tmp_path: Path
    ) -> None:
        newer = make_comment("c1", body="do X too", created_at=_FAR_FUTURE)
        _check(repo, tmp_path, comments=[newer])
        written = json.loads(
            (tmp_path / "scratch" / "impl-comments.json").read_text(encoding="utf-8")
        )
        assert written == [{"created_at": _FAR_FUTURE, "body": "do X too"}]

    def test_an_agent_authored_newer_comment_is_not_stale_and_records_friction(
        self, repo: Path, tmp_path: Path
    ) -> None:
        newer = make_comment("c1", created_at=_FAR_FUTURE, agent_authored=True)
        result = _check(repo, tmp_path, comments=[newer])
        assert result.stale is False
        assert result.reasons == ()
        assert result.operator_comments == ()
        assert result.friction == (AGENT_ONLY_FRICTION,)

    def test_a_minimized_newer_comment_is_not_stale(
        self, repo: Path, tmp_path: Path
    ) -> None:
        newer = make_comment("c1", created_at=_FAR_FUTURE, minimized=True)
        assert _check(repo, tmp_path, comments=[newer]).stale is False

    def test_a_regress_signal_with_only_agent_comments_stays_stale(
        self, repo: Path, tmp_path: Path
    ) -> None:
        newer = make_comment("c1", created_at=_FAR_FUTURE, agent_authored=True)
        result = _check(repo, tmp_path, comments=[newer], signal="impl")
        assert result.stale is True
        assert result.reasons == ("regressed_to_impl",)
        assert result.friction == (AGENT_ONLY_FRICTION,)


class TestOperatorCommentsAfter:
    _HEAD = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)

    def test_keeps_only_operator_comments_newer_than_head(self) -> None:
        keep = make_comment("keep", created_at="2026-11-10T12:00:01Z")
        agent = make_comment("agent", created_at=_FAR_FUTURE, agent_authored=True)
        hidden = make_comment("hidden", created_at=_FAR_FUTURE, minimized=True)
        older = make_comment("older", created_at="2026-11-10T11:59:59Z")
        same = make_comment("same", created_at="2026-11-10T12:00:00Z")
        got = operator_comments_after([keep, agent, hidden, older, same], self._HEAD)
        assert got == (keep,)

    def test_handles_z_and_offset_timestamps(self) -> None:
        offset = make_comment("o", created_at="2026-11-10T13:00:01+02:00")
        later_offset = make_comment("l", created_at="2026-11-10T12:30:00+00:00")
        before = make_comment("b", created_at="2026-11-10T13:59:00+02:00")
        got = operator_comments_after([offset, later_offset, before], self._HEAD)
        assert got == (later_offset,)

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        naive = make_comment("n", created_at="2026-11-10T12:00:01")
        assert operator_comments_after([naive], self._HEAD) == (naive,)

    def test_skips_unparseable_timestamps(self) -> None:
        bad = make_comment("bad", created_at="yesterday")
        blank = make_comment("blank", created_at="  ")
        assert operator_comments_after([bad, blank], self._HEAD) == ()


class TestReadRegressedIntoStage:
    def _write(self, worktree: Path, text: str) -> None:
        target = worktree / HOOK_CONTEXT_RELATIVE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_a_missing_file_is_empty(self, repo: Path) -> None:
        assert read_regressed_into_stage(repo) == ""

    def test_malformed_json_is_empty(self, repo: Path) -> None:
        self._write(repo, "{not json")
        assert read_regressed_into_stage(repo) == ""

    def test_a_non_object_is_empty(self, repo: Path) -> None:
        self._write(repo, "[1, 2]")
        assert read_regressed_into_stage(repo) == ""

    def test_queue_metadata_that_is_not_an_object_is_empty(self, repo: Path) -> None:
        self._write(repo, json.dumps({"queue_metadata": "x"}))
        assert read_regressed_into_stage(repo) == ""

    def test_a_non_string_marker_is_empty(self, repo: Path) -> None:
        self._write(repo, json.dumps({"queue_metadata": {"regressed_into_stage": 3}}))
        assert read_regressed_into_stage(repo) == ""

    def test_a_marker_seeded_through_the_real_writer_is_read(self, repo: Path) -> None:
        task = _task(regressed_into_stage=Stage.IMPL)
        _write_hook_context_file(repo, task=task, ticket_id="T-1", client="test")
        assert read_regressed_into_stage(repo) == "impl"

    def test_a_null_marker_from_the_real_writer_is_empty(self, repo: Path) -> None:
        _write_hook_context_file(repo, task=_task(), ticket_id="T-1", client="test")
        assert read_regressed_into_stage(repo) == ""


class TestRegressSignal:
    def test_the_marker_wins(self, repo: Path) -> None:
        task = _task(regressed_into_stage=Stage.IMPL)
        _write_hook_context_file(repo, task=task, ticket_id="T-1", client="test")
        assert regress_signal(task, repo, merge_present=False) == "impl"

    def test_the_durable_field_counts_while_head_equals_it(self, repo: Path) -> None:
        head = git_in(repo, "rev-parse", "HEAD")
        task = _task(finalize_regress_branch_head=head)
        assert regress_signal(task, repo, merge_present=False) == "impl"

    def test_the_durable_field_is_dead_once_head_moved(self, repo: Path) -> None:
        old = git_in(repo, "rev-parse", "HEAD")
        commit_tracked_file(repo, "b.py")
        task = _task(finalize_regress_branch_head=old)
        assert regress_signal(task, repo, merge_present=False) == ""

    def test_the_durable_field_counts_with_a_merge_present(self, repo: Path) -> None:
        old = git_in(repo, "rev-parse", "HEAD")
        commit_tracked_file(repo, "b.py")
        task = _task(finalize_regress_branch_head=old)
        assert regress_signal(task, repo, merge_present=True) == "impl"

    def test_an_unmeasurable_head_fails_closed(self, repo: Path) -> None:
        task = _task(finalize_regress_branch_head="a" * 40)
        with patch(
            "cw.stage_verification.impl_staleness.current_head_sha", return_value=None
        ):
            assert regress_signal(task, repo, merge_present=False) == "impl"

    def test_no_field_and_no_marker_is_empty(self, repo: Path) -> None:
        assert regress_signal(_task(), repo, merge_present=False) == ""
