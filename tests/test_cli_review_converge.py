"""Tests for ``cw review converge``: the clean-mode convergence CLI (#2787).

Covers ``config``, ``init``, ``suppress``, ``record-round``, ``confirm-failed``,
``status``, ``history-comment`` (``cw.cli.review.converge``), ``verify-tip`` and
``finalize`` (``cw.cli.review.converge_tip``) and ``operator-settle``
(``cw.cli.review.operator_settle``) through Click's ``CliRunner``.

Every ledger a test starts from is built through the real engine
(``tests/_review_convergence_helpers.py``), and every verdict through the real
consolidation path. Git is real: each test worktree is a ``make_git_repo`` clone
on ``feature`` pushed to a bare origin, because ``init`` asks git whether the
ledger's last round is still an ancestor of HEAD and the tip commands fetch.

JSON is read from ``result.stdout`` and messages from ``result.stderr``: Click's
``result.output`` interleaves the two streams.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import TYPE_CHECKING, Any, NoReturn

import pytest
from click.testing import CliRunner
from freezegun import freeze_time

from cw._git import run_git
from cw.auto_dev_result import AutoDevResult
from cw.cli import main
from cw.dev_queue import find_ticket_row
from cw.events import read_events
from cw.models import QueueItemStatus
from cw.models.enums import OrchestratorEventType
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    ConvergenceLedger,
    load_ledger,
)
from tests._cli_review_helpers import (
    _OPERATOR,
    _SETTLE_REASON,
    _consolidate_payload,
    _enter_operator_machine,
    _seed_review_row,
    _settle_entry,
    _settle_payload,
    _write_session_context,
)
from tests._review_convergence_helpers import (
    FORK_POINT,
    LOOP_BACKEND,
    ROSTER,
    TICKET_ID,
    branch_that_merged_main,
    confirmed_ledger,
    consolidated_verdict,
    fresh_ledger,
    ledger_with_rounds,
    operator_finding,
    round_finding,
    round_payload,
    stamp_dispositions,
    stopped_ledger,
    write_ledger,
)
from tests.conftest import (
    _make_finding,
    _make_reviewer_doc,
    commit_tracked_file,
    git_in,
    push_commit_to_origin,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from click.testing import Result

    from cw.models import TicketTask
    from cw.review_convergence import RoundFinding, RoundKind
    from cw.review_findings import ReviewVerdict

_CLIENT = "acme"
_WORKTREE_COMMANDS = {"init", "status", "verify-tip", "finalize", "diff-base"}
_LEDGER_COMMANDS = {"suppress", "record-round", "confirm-failed", "history-comment"}
_RECURRING = "Recurring defect"


@pytest.fixture(autouse=True)
def _operator_machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _enter_operator_machine(monkeypatch, tmp_path)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def wt_origin(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
) -> tuple[Path, Path]:
    """A worktree on ``feature``, pushed to a bare origin."""
    return make_git_repo_with_origin("wt")


@pytest.fixture
def wt(wt_origin: tuple[Path, Path]) -> Path:
    return wt_origin[0]


def _raiser(exc: Exception) -> Callable[..., NoReturn]:
    """A stand-in callable that raises *exc* whatever it is called with."""

    def _raise(*_args: object, **_kwargs: object) -> NoReturn:
        raise exc

    return _raise


def _head(wt: Path) -> str:
    return git_in(wt, "rev-parse", "HEAD")


def _ledger_path(wt: Path) -> Path:
    return wt / LEDGER_RELATIVE_PATH


def _context(
    path: Path,
    *,
    convergence: str = "clean",
    ticket: str | None = TICKET_ID,
    headless: bool = True,
    max_cycles: object = None,
    confirm: object = None,
    schema: object = 15,
) -> Path:
    """Write a cw context shaped like the one ``_write_hook_context`` writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": schema,
                "session_id": "abc",
                "ticket_id": ticket,
                "headless": headless,
                "review": {
                    "convergence": convergence,
                    "max_cycles": max_cycles,
                    "confirm_backend": confirm,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def ctx(tmp_path: Path) -> Path:
    """A schema-15 context for a clean client on a dispatched ticket."""
    return _context(tmp_path / "ctx" / "cw-context.json")


def _converge(
    runner: CliRunner,
    wt: Path,
    sub: str,
    *args: str,
    payload: object = None,
) -> Result:
    """Invoke ``cw review converge <sub>`` against *wt*'s default ledger."""
    location: list[str] = []
    if sub in _WORKTREE_COMMANDS:
        location = ["--worktree", str(wt)]
    elif sub in _LEDGER_COMMANDS:
        location = ["--ledger", str(_ledger_path(wt))]
    return runner.invoke(
        main,
        ["review", "converge", sub, *args, *location],
        input=None if payload is None else json.dumps(payload),
    )


def _out(result: Result) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


def _init(
    runner: CliRunner,
    wt: Path,
    ctx: Path,
    *extra: str,
    ticket: str = TICKET_ID,
    fork_point: str = FORK_POINT,
    sha: str | None = None,
) -> Result:
    return _converge(
        runner,
        wt,
        "init",
        "--ticket-id",
        ticket,
        "--fork-point",
        fork_point,
        "--reviewed-sha",
        sha or _head(wt),
        "--context",
        str(ctx),
        *extra,
    )


def _stored(wt: Path) -> ConvergenceLedger:
    ledger = load_ledger(_ledger_path(wt))
    assert ledger is not None
    return ledger


def _round(
    runner: CliRunner,
    wt: Path,
    kind: RoundKind,
    backend: str,
    sha: str,
    *findings: RoundFinding,
    extra: tuple[str, ...] = (),
) -> Result:
    return _converge(
        runner,
        wt,
        "record-round",
        "-",
        *extra,
        payload=round_payload(kind, backend, sha, *findings),
    )


def _blocked_result(
    blocker: dict[str, object], review_fragment: dict[str, object] | None = None
) -> AutoDevResult:
    """A minimal stage-3 ``blocked`` sentinel around *blocker* (the prose's wrap)."""
    review: dict[str, object] = {
        "must_fix_initial": 0,
        "should_fix": 0,
        "fix_cycles_used": 0,
    }
    review.update(review_fragment or {})
    return AutoDevResult.model_validate(
        {
            "schema_version": 1,
            "ticket_id": TICKET_ID,
            "status": "blocked",
            "stage_reached": "stage3_review",
            "scope": {
                "tier": "small",
                "files": 1,
                "lines_estimate": 10,
                "lines_actual": 10,
                "forbidden_touched": False,
            },
            "plan_source": "linear_existing",
            "branch": "dev/t-1",
            "worktree_path": "/tmp/wt/t-1",
            "fork_point_sha": "aaa",
            "commits": [],
            "pr": None,
            "review": review,
            "health": {
                "lowest_agent_confidence": "LOW",
                "any_incomplete_risk": True,
                "shortcuts": [],
                "recommendation": "EXIT_FOR_HUMAN_REVIEW",
                "downgrade_applied": False,
                "fix_loop_escalated": False,
            },
            "friction_highlights": [],
            "blocker": blocker,
            "next_actions": [],
        }
    )


class TestConfig:
    """``converge config``: what the context says, never an exit code of its own."""

    def _config(self, runner: CliRunner, ctx: Path, *args: str) -> dict[str, Any]:
        result = runner.invoke(
            main, ["review", "converge", "config", "--context", str(ctx), *args]
        )
        assert result.exit_code == 0, result.output
        return _out(result)

    @pytest.mark.parametrize(
        ("schema", "convergence", "source"),
        [
            (14, "clean", "no_review_in_context"),
            (15, "blocking", "context"),
        ],
        ids=["v14-carries-no-review", "blocking-v15"],
    )
    def test_blocking_default_writes_nothing_and_init_is_a_noop(
        self,
        runner: CliRunner,
        wt: Path,
        tmp_path: Path,
        schema: int,
        convergence: str,
        source: str,
    ) -> None:
        ctx = _context(tmp_path / "ctx.json", schema=schema, convergence=convergence)
        report = self._config(runner, ctx)
        assert report["convergence"] == "blocking"
        assert report["source"] == source
        init = _init(runner, wt, ctx)
        assert init.exit_code == 0, init.output
        assert _out(init) == {"convergence": "blocking", "status": "noop"}
        assert not _ledger_path(wt).exists()
        assert not (wt / ".cw").exists()

    @pytest.mark.parametrize(
        "shape",
        [
            "missing",
            "directory",
            "invalid_json",
            "list",
            "review_not_object",
            "non_utf8",
            "schema_not_int",
            "schema_bool",
        ],
    )
    def test_never_exits_non_zero_for_a_context_shape(
        self, runner: CliRunner, wt: Path, tmp_path: Path, shape: str
    ) -> None:
        target = tmp_path / "ctx.json"
        if shape == "directory":
            target.mkdir()
        elif shape == "invalid_json":
            target.write_text("{not json", encoding="utf-8")
        elif shape == "list":
            target.write_text("[]", encoding="utf-8")
        elif shape == "review_not_object":
            target.write_text(
                json.dumps({"schema_version": 15, "review": "clean"}),
                encoding="utf-8",
            )
        elif shape == "non_utf8":
            target.write_bytes(b"\xff\xfe\x00bad")
        elif shape == "schema_not_int":
            _context(target, schema="15")
        elif shape == "schema_bool":
            _context(target, schema=True)
        report = self._config(runner, target)
        assert report["convergence"] == "blocking"
        assert report["source"] == "no_review_in_context"
        assert report["dispatch_refusal"] is None
        assert not (wt / ".cw").exists()

    def test_default_context_path_is_the_cwd_cw_context(
        self, runner: CliRunner
    ) -> None:
        """The operator-machine context (schema 8) carries no review object."""
        result = runner.invoke(main, ["review", "converge", "config"])
        assert result.exit_code == 0, result.output
        assert _out(result)["source"] == "no_review_in_context"

    def test_clean_context_reports_the_resolved_settings(
        self, runner: CliRunner, ctx: Path
    ) -> None:
        report = self._config(runner, ctx)
        assert report == {
            "convergence": "clean",
            "max_cycles": 8,
            "confirm_backend": "codex",
            "confirm_backend_configured": None,
            "confirm_not_independent": False,
            "loop_backend": LOOP_BACKEND,
            "source": "context",
            "ticket_id": TICKET_ID,
            "dispatch_refusal": None,
        }

    def test_codex_loop_resolves_the_claude_confirm_backend(
        self, runner: CliRunner, ctx: Path
    ) -> None:
        report = self._config(runner, ctx, "--loop-backend", "codex")
        assert report["confirm_backend"] == "claude"
        assert report["loop_backend"] == "codex"

    def test_explicit_claude_confirm_on_claude_loop_is_not_independent(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        ctx = _context(tmp_path / "c.json", confirm="claude", max_cycles=12)
        report = self._config(runner, ctx)
        assert report["confirm_not_independent"] is True
        assert report["confirm_backend_configured"] == "claude"
        assert report["max_cycles"] == 12

    def test_explicit_codex_and_none_confirm_backends_pass_through(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        for backend in ("codex", "none"):
            ctx = _context(tmp_path / f"{backend}.json", confirm=backend)
            report = self._config(runner, ctx)
            assert report["confirm_backend"] == backend
            assert report["confirm_backend_configured"] == backend
            assert report["confirm_not_independent"] is False

    @pytest.mark.parametrize("cycles", [0, "many", True, -3, None])
    def test_invalid_cycle_caps_read_as_the_clean_default(
        self, runner: CliRunner, tmp_path: Path, cycles: object
    ) -> None:
        ctx = _context(tmp_path / "c.json", max_cycles=cycles)
        assert self._config(runner, ctx)["max_cycles"] == 8

    def test_a_drifted_context_still_reads_clean(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """An unknown confirm backend reads as unset; it never downgrades clean."""
        ctx = _context(tmp_path / "c.json", confirm="bogus", max_cycles=None)
        report = self._config(runner, ctx)
        assert report["convergence"] == "clean"
        assert report["confirm_backend_configured"] is None

    def test_blocking_context_reports_the_blocking_cap(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        ctx = _context(tmp_path / "c.json", convergence="blocking")
        assert self._config(runner, ctx)["max_cycles"] == 5

    @pytest.mark.parametrize(
        "ticket", [None, "", "   "], ids=["null", "empty", "blank"]
    )
    @pytest.mark.parametrize("headless", [False, True], ids=["interactive", "headless"])
    def test_clean_client_with_no_usable_ticket_refuses(
        self,
        runner: CliRunner,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        ticket: str | None,
        headless: bool,
    ) -> None:
        ctx = _context(tmp_path / "c.json", ticket=ticket, headless=headless)
        report = self._config(runner, ctx)
        assert report["convergence"] == "clean"
        assert report["source"] == "context"
        assert report["ticket_id"] == ticket
        refusal = report["dispatch_refusal"]
        assert isinstance(refusal, str)
        assert "cw dev-queue" in refusal
        # The message is the `details` of the agent_block sentinel the headless
        # run-start guards emit; it must validate with a known reason.
        with caplog.at_level("WARNING", logger="cw.auto_dev_result"):
            result = _blocked_result(
                {
                    "stage": "stage3_review",
                    "reason": "agent_block",
                    "details": refusal,
                    "retry_eligible": False,
                }
            )
        assert result.blocker is not None
        assert result.blocker.details == refusal
        assert "blocker_reason_unknown" not in caplog.text

    def test_a_dispatched_ticket_does_not_refuse(
        self, runner: CliRunner, ctx: Path
    ) -> None:
        assert self._config(runner, ctx)["dispatch_refusal"] is None

    def test_a_null_ticket_on_a_blocking_client_is_unchanged(
        self, runner: CliRunner, tmp_path: Path, wt: Path
    ) -> None:
        ctx = _context(tmp_path / "c.json", convergence="blocking", ticket=None)
        report = self._config(runner, ctx)
        assert report["convergence"] == "blocking"
        assert report["dispatch_refusal"] is None
        assert not (wt / ".cw").exists()


def _dirty(count: int, tag: str) -> list[RoundFinding]:
    return [round_finding(summary=f"{tag} {n}") for n in range(count)]


class TestInit:
    """``converge init``: reconcile the ledger at every session start."""

    def test_creates_the_ledger_from_the_context(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        result = _init(runner, wt, ctx)
        assert result.exit_code == 0, result.output
        report = _out(result)
        assert report["status"] == "in_progress"
        assert report["reset_reason"] is None
        assert report["max_cycles"] == 8
        assert "released" not in report
        ledger = _stored(wt)
        assert ledger.checkpoint_3a_sha == _head(wt)
        assert ledger.confirm_backend == "codex"
        assert ledger.ticket_id == TICKET_ID
        assert ledger.fork_point == FORK_POINT

    def test_codex_loop_stamps_codex_loop_and_claude_confirm(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        """#2791: a codex loop resolves the independent claude confirm."""
        result = _init(runner, wt, ctx, "--loop-backend", "codex")
        assert result.exit_code == 0, result.output
        ledger = _stored(wt)
        assert ledger.loop_backend == "codex"
        assert ledger.confirm_backend == "claude"
        assert ledger.confirm_not_independent is False

    def test_no_confirm_records_no_confirm_round(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        result = _init(
            runner,
            wt,
            ctx,
            "--no-confirm",
            "--ledger",
            ".cw/prep-pr-convergence.json",
        )
        assert result.exit_code == 0, result.output
        ledger = load_ledger(wt / ".cw/prep-pr-convergence.json")
        assert ledger is not None
        assert ledger.confirm_backend == "none"

    def test_no_confirm_on_the_default_ledger_is_refused(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        result = _init(runner, wt, ctx, "--no-confirm")
        assert result.exit_code != 0
        assert "--no-confirm is only valid" in result.output
        assert not _ledger_path(wt).exists()

    def test_is_idempotent(self, runner: CliRunner, wt: Path, ctx: Path) -> None:
        _init(runner, wt, ctx)
        before = _ledger_path(wt).read_bytes()
        again = _init(runner, wt, ctx)
        assert again.exit_code == 0, again.output
        assert _ledger_path(wt).read_bytes() == before

    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            ({"ticket": "T-OTHER"}, "ticket_mismatch"),
            ({"fork_point": "otherfork"}, "fork_point_mismatch"),
        ],
    )
    def test_a_ledger_for_another_ticket_or_fork_point_is_discarded(
        self,
        runner: CliRunner,
        wt: Path,
        ctx: Path,
        kwargs: dict[str, str],
        reason: str,
    ) -> None:
        write_ledger(wt, ledger_with_rounds(_dirty(1, "x"), shas=[_head(wt)]))
        result = _init(runner, wt, ctx, **kwargs)
        assert result.exit_code == 0, result.output
        assert _out(result)["reset_reason"] == reason
        assert _stored(wt).rounds == []

    def test_a_rewritten_branch_discards_the_ledger(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        """The last round's sha is not an ancestor of HEAD (a regress rewrote it)."""
        commit_tracked_file(wt, "rewritten.py")
        orphan = _head(wt)
        git_in(wt, "reset", "--hard", "HEAD~1")
        write_ledger(wt, ledger_with_rounds(_dirty(1, "x"), shas=[orphan]))
        result = _init(runner, wt, ctx)
        assert result.exit_code == 0, result.output
        assert _out(result)["reset_reason"] == "history_rewritten"
        assert _stored(wt).rounds == []

    def test_a_git_failure_is_an_error_not_a_history_rewrite(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        """An unknown object is exit 128: git failed, the history said nothing."""
        write_ledger(wt, ledger_with_rounds(_dirty(1, "x"), shas=["0" * 40]))
        before = _ledger_path(wt).read_bytes()
        result = _init(runner, wt, ctx)
        assert result.exit_code == 1
        assert "git merge-base --is-ancestor failed" in result.stderr
        assert "exit 128" in result.stderr
        assert isinstance(result.exception, SystemExit)
        assert _ledger_path(wt).read_bytes() == before

    def test_a_round_on_an_ancestor_resumes(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        old = _head(wt)
        write_ledger(wt, ledger_with_rounds(_dirty(1, "x"), shas=[old]))
        commit_tracked_file(wt, "next.py")
        result = _init(runner, wt, ctx)
        assert result.exit_code == 0, result.output
        assert _out(result)["reset_reason"] is None
        assert len(_stored(wt).rounds) == 1

    def test_git_that_cannot_run_is_an_error_not_a_traceback(
        self, runner: CliRunner, wt: Path, ctx: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_ledger(wt, ledger_with_rounds(_dirty(1, "x"), shas=[_head(wt)]))

        # #2791: the merge-base call moved with ``history_rewritten`` into the
        # engine package, so the seam is that module's ``run_git``.
        monkeypatch.setattr(
            "cw.review_convergence._release.run_git",
            _raiser(FileNotFoundError("git")),
        )
        result = _init(runner, wt, ctx)
        assert result.exit_code == 1
        assert "git merge-base could not run" in result.stderr
        assert isinstance(result.exception, SystemExit)

    def test_an_unreadable_ledger_is_a_one_line_error(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        path = _ledger_path(wt)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        result = _init(runner, wt, ctx)
        assert result.exit_code == 1
        assert "cannot read the convergence ledger" in result.stderr
        assert "Traceback" not in result.output
        assert isinstance(result.exception, SystemExit)

    def test_cap_refresh_on_a_new_ledger_and_a_resumed_one(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        low = _context(tmp_path / "low.json", max_cycles=8)
        high = _context(tmp_path / "high.json", max_cycles=12)
        _init(runner, wt, low)
        assert _stored(wt).max_cycles == 8
        report = _out(_init(runner, wt, high))
        assert report["max_cycles"] == 12
        assert _stored(wt).max_cycles == 12
        recorded = _round(runner, wt, "loop", LOOP_BACKEND, _head(wt), *_dirty(2, "a"))
        assert recorded.exit_code == 0, recorded.output
        resumed = _out(_init(runner, wt, low))
        assert resumed["max_cycles"] == 8
        assert _stored(wt).max_cycles == 8


def _verdict_round(
    kind: RoundKind, backend: str, sha: str, verdict: ReviewVerdict
) -> dict[str, object]:
    """A ``record-round`` verdict-form payload."""
    return {
        "kind": kind,
        "backend": backend,
        "reviewed_sha": sha,
        "roster": list(ROSTER),
        "verdict": json.loads(verdict.model_dump_json()),
    }


def _fixed_verdict(sha: str, *summaries: str) -> ReviewVerdict:
    """A consolidated verdict whose accepted findings are all adjudicated ``fixed``."""
    verdict = consolidated_verdict(
        accepted=[("MUST_FIX", summary) for summary in summaries], sha=sha
    )
    return stamp_dispositions(verdict, dict.fromkeys(summaries, ("fixed", "")))


def _record_verdict(
    runner: CliRunner, wt: Path, payload: dict[str, object], *extra: str
) -> Result:
    return _converge(runner, wt, "record-round", "-", *extra, payload=payload)


class TestStopRelease:
    """Same-tip requeue after a stop (R10 to R13)."""

    def _drive_to_max_cycles(self, runner: CliRunner, wt: Path, ctx: Path) -> None:
        assert _init(runner, wt, ctx).exit_code == 0
        for sha, count in (("s1", 2), ("s2", 1)):
            step = _round(runner, wt, "loop", LOOP_BACKEND, sha, *_dirty(count, sha))
            assert _out(step)["decision"] == "continue"
        last = _round(runner, wt, "loop", LOOP_BACKEND, _head(wt), *_dirty(2, "z"))
        assert _out(last)["decision"] == "stop_max_cycles"

    def test_max_cycles_stop_is_reemitted_then_released_by_a_raised_cap(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        low = _context(tmp_path / "low.json", max_cycles=2)
        high = _context(tmp_path / "high.json", max_cycles=12)
        self._drive_to_max_cycles(runner, wt, low)
        before = _ledger_path(wt).read_bytes()

        again = _out(_init(runner, wt, low))
        assert again["status"] == "stopped"
        assert again["reemitted"] is True
        assert "released" not in again
        assert _ledger_path(wt).read_bytes() == before

        released = _out(_init(runner, wt, high))
        assert released["released"]["cause"] == "max_cycles_raised"
        assert released["status"] == "in_progress"
        assert released["max_cycles"] == 12
        status = _out(_converge(runner, wt, "status"))
        assert status["status"] == "in_progress"
        assert status["next_action"] == "loop_round"
        more = _round(runner, wt, "loop", LOOP_BACKEND, _head(wt), *_dirty(1, "q"))
        assert _out(more)["decision"] == "continue"

    def test_operator_action_stop_releases_on_the_same_tip(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        _init(runner, wt, ctx)
        stop = _round(runner, wt, "loop", LOOP_BACKEND, _head(wt), operator_finding())
        assert _out(stop)["decision"] == "stop_operator_action"
        released = _out(_init(runner, wt, ctx))
        assert released["released"]["cause"] == "operator_action_done"
        status = _out(_converge(runner, wt, "status"))
        assert status["next_action"] == "loop_round"

    @pytest.mark.parametrize("reason", ["no_progress", "oscillation"])
    def test_other_stops_stay_terminal_even_with_a_raised_cap(
        self, runner: CliRunner, wt: Path, tmp_path: Path, reason: str
    ) -> None:
        write_ledger(wt, stopped_ledger(reason, sha=_head(wt)))
        raised = _context(tmp_path / "raised.json", max_cycles=40)
        report = _out(_init(runner, wt, raised))
        assert report["reemitted"] is True
        assert report["stop"]["releasable"] is False
        status = _out(_converge(runner, wt, "status"))
        assert status["next_action"] == "stopped"
        assert status["stop"]["releasable"] is False
        assert "--regress" in status["stop"]["release_hint"]
        assert status["stop"]["reason"] == reason
        # init refreshed the cap but nothing else: the stop is untouched.
        stored = _stored(wt)
        assert stored.status == "stopped"
        assert stored.max_cycles == 40

    def test_new_commits_reopen_a_stopped_ledger(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        write_ledger(wt, stopped_ledger("no_progress", sha=_head(wt)))
        commit_tracked_file(wt, "fix.py")
        report = _out(_init(runner, wt, ctx))
        assert report["released"]["cause"] == "new_commits"
        assert report["status"] == "in_progress"

    def test_a_converged_ledger_on_a_moved_tip_reopens(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        write_ledger(wt, confirmed_ledger(sha=_head(wt)))
        commit_tracked_file(wt, "merge-of-main.py")
        report = _out(_init(runner, wt, ctx))
        assert report["status"] == "in_progress"
        assert "released" not in report
        status = _out(_converge(runner, wt, "status"))
        assert status["next_action"] == "loop_round"

    def test_a_converged_ledger_on_the_same_tip_is_done(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        write_ledger(wt, confirmed_ledger(sha=_head(wt)))
        _init(runner, wt, ctx)
        status = _out(_converge(runner, wt, "status"))
        assert status["next_action"] == "done"


class TestStatus:
    def test_recovers_state_across_a_session_boundary(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        head = _head(wt)
        _init(runner, wt, ctx)
        recorded = _record_verdict(
            runner,
            wt,
            _verdict_round("loop", LOOP_BACKEND, head, _fixed_verdict(head, "Bug A")),
        )
        assert _out(recorded)["decision"] == "continue"
        # A new session: a new runner, nothing in memory but the files.
        status = _out(_converge(CliRunner(), wt, "status"))
        assert status["checkpoint_3a_sha"] == head
        assert status["frozen"]["must_fix_initial"] == 1
        assert status["next_action"] == "fix"
        assert status["next_fix_cycle"] == 1
        assert status["roster"] == ROSTER
        assert status["stop"] is None
        assert status["sentinel_review"]["convergence_mode"] == "clean"
        assert status["sentinel_review"]["converged"] is False

    def test_resume_routing_confirm_then_loop_round(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        head = _head(wt)
        _init(runner, wt, ctx)
        report = _out(_round(runner, wt, "loop", LOOP_BACKEND, head))
        assert report["decision"] == "confirm_required"
        status = _out(_converge(runner, wt, "status"))
        assert status["status"] == "awaiting_confirm"
        assert status["awaiting_confirm_sha"] == head
        assert status["next_action"] == "confirm"
        commit_tracked_file(wt, "moved.py")
        moved = _out(_converge(runner, wt, "status"))
        assert moved["next_action"] == "loop_round"

    def test_a_stopped_ledger_carries_the_stop_detail(
        self, runner: CliRunner, wt: Path
    ) -> None:
        write_ledger(
            wt,
            stopped_ledger("oscillation", sha=_head(wt), stop_identities=(_RECURRING,)),
        )
        status = _out(_converge(runner, wt, "status"))
        stop = status["stop"]
        assert set(stop) == {
            "reason",
            "identities",
            "settle_payload",
            "releasable",
            "release_hint",
        }
        assert stop["reason"] == "oscillation"
        assert stop["identities"][0]["summary_normalized"] == _RECURRING
        assert stop["settle_payload"]["entries"][0]["summary"] == _RECURRING
        assert status["next_action"] == "stopped"
        assert status["releases"] == []

    def test_a_missing_ledger_is_a_one_line_error(
        self, runner: CliRunner, wt: Path
    ) -> None:
        result = _converge(runner, wt, "status")
        assert result.exit_code == 1
        assert "no convergence ledger" in result.stderr
        assert isinstance(result.exception, SystemExit)

    def test_git_failing_in_the_worktree_is_a_one_line_error(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        plain = tmp_path / "plain"
        write_ledger(plain, fresh_ledger())
        result = _converge(runner, plain, "status")
        assert result.exit_code == 1
        assert "git rev-parse HEAD failed" in result.stderr

    def test_git_that_cannot_run_is_a_one_line_error(
        self, runner: CliRunner, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_ledger(wt, fresh_ledger())

        monkeypatch.setattr(
            "cw.cli.review.converge.run_git", _raiser(FileNotFoundError("git"))
        )
        result = _converge(runner, wt, "status")
        assert result.exit_code == 1
        assert "could not run" in result.stderr


def _diff_base_context(path: Path, branch: object) -> Path:
    """A clean-client context whose world-state snapshot names *branch*."""
    _context(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["world_state_snapshot"] = {"origin_main_branch": branch}
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _publish_develop(wt: Path) -> None:
    """Publish ``origin/main``'s commit as ``origin/develop``."""
    git_in(wt, "push", "origin", "refs/remotes/origin/main:refs/heads/develop")


class TestDiffBase:
    """``converge diff-base``: the one review diff base, pinned (#2800)."""

    def test_pins_the_merge_base_and_reports_the_change(
        self, runner: CliRunner, wt: Path, ctx: Path, tmp_path: Path
    ) -> None:
        merged = branch_that_merged_main(wt, tmp_path / "main-clone")
        _init(runner, wt, ctx, fork_point=merged.fork_point)

        result = _converge(runner, wt, "diff-base")

        assert result.exit_code == 0, result.stderr
        assert _out(result) == {
            "diff_base": merged.merge_base,
            "default_branch": "main",
            "previous": None,
            "changed": True,
        }
        stored = _stored(wt)
        assert stored.diff_base == merged.merge_base
        assert stored.fork_point == merged.fork_point

    def test_a_second_run_at_the_same_head_changes_nothing(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        fork = git_in(wt, "merge-base", "origin/main", "HEAD")
        _init(runner, wt, ctx, fork_point=fork)
        assert _converge(runner, wt, "diff-base").exit_code == 0
        before = _ledger_path(wt).read_bytes()

        again = _converge(runner, wt, "diff-base")

        assert again.exit_code == 0, again.stderr
        assert _out(again) == {
            "diff_base": fork,
            "default_branch": "main",
            "previous": fork,
            "changed": False,
        }
        assert _ledger_path(wt).read_bytes() == before

    def test_a_merge_of_main_moves_the_pin_but_not_the_fork_point(
        self, runner: CliRunner, wt: Path, ctx: Path, tmp_path: Path
    ) -> None:
        fork = git_in(wt, "merge-base", "origin/main", "HEAD")
        _init(runner, wt, ctx, fork_point=fork)
        assert _out(_converge(runner, wt, "diff-base"))["diff_base"] == fork

        merged = branch_that_merged_main(wt, tmp_path / "main-clone")
        moved = _out(_converge(runner, wt, "diff-base"))

        assert moved == {
            "diff_base": merged.merge_base,
            "default_branch": "main",
            "previous": fork,
            "changed": True,
        }
        assert _stored(wt).fork_point == fork
        # The next session's init keeps the ledger: no reset, the pin survives.
        reinit = _init(runner, wt, ctx, fork_point=fork)
        assert reinit.exit_code == 0, reinit.stderr
        assert _out(reinit)["reset_reason"] is None
        assert _stored(wt).diff_base == merged.merge_base

    def test_fetches_the_default_branch_before_resolving(
        self, runner: CliRunner, wt: Path, ctx: Path, tmp_path: Path
    ) -> None:
        merged = branch_that_merged_main(wt, tmp_path / "main-clone")
        _init(runner, wt, ctx, fork_point=merged.fork_point)
        # The tracking ref predates the push, as an unfetched clone holds it.
        git_in(wt, "update-ref", "refs/remotes/origin/main", merged.fork_point)

        result = _converge(runner, wt, "diff-base")

        assert result.exit_code == 0, result.stderr
        assert _out(result)["diff_base"] == merged.merge_base
        assert git_in(wt, "rev-parse", "origin/main") != merged.fork_point

    def test_no_ledger_is_a_one_line_error(self, runner: CliRunner, wt: Path) -> None:
        result = _converge(runner, wt, "diff-base")

        assert result.exit_code == 1
        assert "run 'cw review converge init' first" in result.stderr
        assert not _ledger_path(wt).exists()

    def test_a_failing_fetch_writes_nothing(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        _init(runner, wt, ctx)
        before = _ledger_path(wt).read_bytes()
        git_in(wt, "remote", "remove", "origin")

        result = _converge(runner, wt, "diff-base")

        assert result.exit_code == 1
        assert "git fetch origin main failed" in result.stderr
        assert _ledger_path(wt).read_bytes() == before

    def test_the_option_overrides_the_context(
        self, runner: CliRunner, wt: Path, ctx: Path, tmp_path: Path
    ) -> None:
        _init(runner, wt, ctx)
        _publish_develop(wt)
        context = _diff_base_context(tmp_path / "ctx2" / "cw-context.json", "nope")

        result = _converge(
            runner,
            wt,
            "diff-base",
            "--default-branch",
            "develop",
            "--context",
            str(context),
        )

        assert result.exit_code == 0, result.stderr
        assert _out(result)["default_branch"] == "develop"
        assert _out(result)["diff_base"] == git_in(
            wt, "merge-base", "origin/develop", "HEAD"
        )

    def test_the_context_branch_is_used_without_the_option(
        self, runner: CliRunner, wt: Path, ctx: Path, tmp_path: Path
    ) -> None:
        _init(runner, wt, ctx)
        _publish_develop(wt)
        context = _diff_base_context(tmp_path / "ctx2" / "cw-context.json", "develop")

        result = _converge(runner, wt, "diff-base", "--context", str(context))

        assert result.exit_code == 0, result.stderr
        assert _out(result)["default_branch"] == "develop"

    @pytest.mark.parametrize("branch", [None, "", 7], ids=["null", "empty", "int"])
    def test_an_unusable_context_branch_falls_back_to_main(
        self, runner: CliRunner, wt: Path, ctx: Path, tmp_path: Path, branch: object
    ) -> None:
        _init(runner, wt, ctx)
        context = _diff_base_context(tmp_path / "ctx2" / "cw-context.json", branch)

        result = _converge(runner, wt, "diff-base", "--context", str(context))

        assert result.exit_code == 0, result.stderr
        assert _out(result)["default_branch"] == "main"

    def test_no_context_defaults_to_main(
        self, runner: CliRunner, wt: Path, ctx: Path, tmp_path: Path
    ) -> None:
        _init(runner, wt, ctx)

        result = _converge(
            runner, wt, "diff-base", "--context", str(tmp_path / "absent.json")
        )

        assert result.exit_code == 0, result.stderr
        assert _out(result)["default_branch"] == "main"

    def test_a_merge_after_the_clean_loop_round_needs_another_round(
        self, runner: CliRunner, wt: Path, ctx: Path, tmp_path: Path
    ) -> None:
        """The strict tip rule is what makes the re-pin sound (#2800)."""
        head = _head(wt)
        fork = git_in(wt, "merge-base", "origin/main", "HEAD")
        _init(runner, wt, ctx, fork_point=fork)
        assert _out(_converge(runner, wt, "diff-base"))["diff_base"] == fork
        report = _out(_round(runner, wt, "loop", LOOP_BACKEND, head))
        assert report["decision"] == "confirm_required"
        assert _out(_converge(runner, wt, "status"))["next_action"] == "confirm"

        merged = branch_that_merged_main(wt, tmp_path / "main-clone")

        assert _out(_converge(runner, wt, "status"))["next_action"] == "loop_round"
        assert _out(_converge(runner, wt, "diff-base"))["diff_base"] == (
            merged.merge_base
        )

    def test_verify_tip_refuses_a_converged_ledger_after_a_merge(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        write_ledger(wt, confirmed_ledger(sha=_head(wt)))

        branch_that_merged_main(wt, tmp_path / "main-clone")
        verify = _converge(runner, wt, "verify-tip")

        assert verify.exit_code == 1
        assert _out(verify) == {
            "decision": "rereview_required",
            "reason": "commits_after_clean_round",
        }

    def test_a_fork_point_reset_starts_without_a_pin(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        _init(runner, wt, ctx)
        assert _converge(runner, wt, "diff-base").exit_code == 0
        assert _stored(wt).diff_base is not None

        reset = _init(runner, wt, ctx, fork_point="other-fork")

        assert _out(reset)["reset_reason"] == "fork_point_mismatch"
        assert _stored(wt).diff_base is None

    def test_help_names_the_contract(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["review", "converge", "diff-base", "--help"])

        assert result.exit_code == 0
        flat = " ".join(result.output.split())
        assert "merge-base origin/<default> HEAD" in flat
        assert "the fork point stays the ledger's identity key" in flat
        for option in ("--default-branch", "--context", "--worktree", "--ledger"):
            assert option in flat


class TestSuppress:
    def _settled(self, runner: CliRunner, wt: Path, ctx: Path) -> str:
        """A ledger that settled "Known" (reject) and is still in progress."""
        head = _head(wt)
        _init(runner, wt, ctx)
        done = _round(
            runner,
            wt,
            "loop",
            LOOP_BACKEND,
            head,
            round_finding(summary="Known", outcome="reject", rationale="intentional"),
            round_finding(summary="Other"),
        )
        assert _out(done)["decision"] == "continue"
        return head

    def test_findings_form_stamps_a_resurfaced_settled_finding(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        self._settled(runner, wt, ctx)
        findings = [
            round_finding(summary="Known"),
            round_finding(summary="Brand new"),
        ]
        result = _converge(
            runner,
            wt,
            "suppress",
            "-",
            payload={"findings": [f.model_dump(mode="json") for f in findings]},
        )
        assert result.exit_code == 0, result.stderr
        out = _out(result)
        assert out["suppressed"] == 1
        assert out["findings"][0]["outcome"] == "reject"
        assert out["findings"][0]["rationale"] == "intentional"
        assert out["findings"][1]["outcome"] == "fix"
        assert out["adjudications"][0]["rationale"] == "intentional"
        assert out["adjudications"][0]["outcome"] == "reject"
        assert "verdict" not in out

    def test_verdict_form_stamps_and_recomputes_must_fix(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        head = _head(wt)
        _init(runner, wt, ctx)
        settled = stamp_dispositions(
            consolidated_verdict(
                accepted=[("MUST_FIX", "Bug A"), ("SHOULD_FIX", "Keep")], sha=head
            ),
            {"Bug A": ("rejected", "fine as is"), "Keep": ("fixed", "")},
        )
        recorded = _record_verdict(
            runner, wt, _verdict_round("loop", LOOP_BACKEND, head, settled)
        )
        assert _out(recorded)["decision"] == "continue"

        fresh = consolidated_verdict(accepted=[("MUST_FIX", "Bug A")], sha=head)
        assert fresh.blocking
        result = _converge(
            runner,
            wt,
            "suppress",
            "-",
            payload={"verdict": json.loads(fresh.model_dump_json())},
        )
        assert result.exit_code == 0, result.stderr
        out = _out(result)
        verdict = out["verdict"]
        assert out["suppressed"] == 1
        assert verdict["accepted"][0]["disposition"] == "rejected"
        assert verdict["accepted"][0]["disposition_detail"] == "fine as is"
        assert verdict["must_fix"] == []
        assert verdict["blocking"] is False
        assert out["adjudications"][0]["rationale"] == "fine as is"

    def test_nothing_resurfaced_echoes_the_payload(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        self._settled(runner, wt, ctx)
        finding = round_finding(summary="Brand new").model_dump(mode="json")
        result = _converge(runner, wt, "suppress", "-", payload={"findings": [finding]})
        out = _out(result)
        assert out == {"findings": [finding], "adjudications": [], "suppressed": 0}

    @pytest.mark.parametrize(
        "payload",
        [{}, {"findings": [], "verdict": {}}, {"nonsense": 1}],
        ids=["neither", "both", "unknown-key"],
    )
    def test_requires_exactly_one_of_verdict_or_findings(
        self, runner: CliRunner, wt: Path, payload: dict[str, object]
    ) -> None:
        write_ledger(wt, fresh_ledger())
        result = _converge(runner, wt, "suppress", "-", payload=payload)
        assert result.exit_code == 1
        assert result.stdout == ""

    def test_a_missing_ledger_is_a_one_line_error(
        self, runner: CliRunner, wt: Path
    ) -> None:
        result = _converge(runner, wt, "suppress", "-", payload={"findings": []})
        assert result.exit_code == 1
        assert "no convergence ledger" in result.stderr


class TestRecordRound:
    def test_accepts_both_payload_shapes_and_exactly_one(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        head = _head(wt)
        _init(runner, wt, ctx)
        findings = _round(runner, wt, "loop", LOOP_BACKEND, head, *_dirty(1, "f"))
        assert findings.exit_code == 0, findings.stderr
        verdict = _record_verdict(
            runner,
            wt,
            _verdict_round("loop", LOOP_BACKEND, head, _fixed_verdict(head, "Bug A")),
        )
        assert verdict.exit_code == 0, verdict.stderr
        both = _verdict_round("loop", LOOP_BACKEND, head, _fixed_verdict(head, "x"))
        both["findings"] = []
        refused = _record_verdict(runner, wt, both)
        assert refused.exit_code == 1
        assert "exactly one" in refused.stderr
        assert len(_stored(wt).rounds) == 2

    def test_a_verdict_round_persists_the_verdict_file_and_out_verdict(
        self, runner: CliRunner, wt: Path, ctx: Path, tmp_path: Path
    ) -> None:
        head = _head(wt)
        _init(runner, wt, ctx)
        out = tmp_path / "elsewhere" / "verdict.json"
        result = _record_verdict(
            runner,
            wt,
            _verdict_round("loop", LOOP_BACKEND, head, _fixed_verdict(head, "Bug A")),
            "--out-verdict",
            str(out),
        )
        assert result.exit_code == 0, result.stderr
        report = _out(result)
        assert report["round"] == 1
        stored = wt / ".cw" / "review-convergence" / "round-1-verdict.json"
        assert json.loads(stored.read_text(encoding="utf-8"))["reviewed_sha"] == head
        assert out.read_text(encoding="utf-8") == stored.read_text(encoding="utf-8")

    def test_a_failed_ledger_commit_is_recovered_by_the_next_call(
        self,
        runner: CliRunner,
        wt: Path,
        ctx: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ledger write is the commit point: dying before it loses nothing."""
        head = _head(wt)
        _init(runner, wt, ctx)
        before = _ledger_path(wt).read_bytes()
        out = tmp_path / "verdict.json"
        payload = _verdict_round(
            "loop", LOOP_BACKEND, head, _fixed_verdict(head, "Bug A")
        )
        with monkeypatch.context() as crash:
            crash.setattr(
                "cw.cli.review.converge.save_ledger",
                _raiser(OSError("No space left on device")),
            )
            failed = _record_verdict(runner, wt, payload, "--out-verdict", str(out))
        assert failed.exit_code == 1
        assert isinstance(failed.exception, OSError)
        assert _ledger_path(wt).read_bytes() == before
        assert _stored(wt).rounds == []

        retried = _record_verdict(runner, wt, payload, "--out-verdict", str(out))
        assert retried.exit_code == 0, retried.stderr
        assert _out(retried)["round"] == 1
        assert len(_stored(wt).rounds) == 1
        stored = wt / ".cw" / "review-convergence" / "round-1-verdict.json"
        assert json.loads(stored.read_text(encoding="utf-8"))["reviewed_sha"] == head
        assert out.read_text(encoding="utf-8") == stored.read_text(encoding="utf-8")
        assert len(list(stored.parent.glob("round-*-verdict.json"))) == 1

    def test_a_findings_round_writes_no_verdict_file(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        _init(runner, wt, ctx)
        _round(runner, wt, "loop", LOOP_BACKEND, _head(wt), *_dirty(1, "f"))
        assert not (wt / ".cw" / "review-convergence").exists()

    def test_a_clean_loop_round_then_a_clean_confirm_converges(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        head = _head(wt)
        _init(runner, wt, ctx)
        loop = _out(_round(runner, wt, "loop", LOOP_BACKEND, head))
        assert loop["decision"] == "confirm_required"
        confirm = _out(_round(runner, wt, "confirm", "codex", head))
        assert confirm["decision"] == "clean"
        assert _stored(wt).status == "converged"

    def test_a_confirm_round_with_a_finding_continues_the_loop(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        head = _head(wt)
        _init(runner, wt, ctx)
        _round(runner, wt, "loop", LOOP_BACKEND, head)
        found = _out(_round(runner, wt, "confirm", "codex", head, *_dirty(1, "c")))
        assert found["decision"] == "continue"
        assert _stored(wt).awaiting_confirm_sha is None

    def test_opt_out_clean_round_converges_and_finalizes_with_the_choice_visible(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        optout = _context(tmp_path / "optout.json", confirm="none")
        head = _head(wt)
        _init(runner, wt, optout)
        assert (
            _out(_round(runner, wt, "loop", LOOP_BACKEND, head))["decision"] == "clean"
        )
        final = _converge(runner, wt, "finalize")
        assert final.exit_code == 0, final.stdout + final.stderr
        fragment = _out(final)["sentinel_review"]
        assert fragment["convergence_mode"] == "clean"
        assert fragment["confirm_backend"] == "none"
        assert (
            "confirm_round_opt_out: review.confirm_backend=none"
            in _out(final)["friction_lines"]
        )

    def test_a_missing_ledger_is_a_one_line_error(
        self, runner: CliRunner, wt: Path
    ) -> None:
        result = _round(runner, wt, "loop", LOOP_BACKEND, _head(wt))
        assert result.exit_code == 1
        assert "no convergence ledger" in result.stderr
        assert isinstance(result.exception, SystemExit)


def _assert_nothing_recorded(wt: Path, before: bytes, out_verdict: Path) -> None:
    assert _ledger_path(wt).read_bytes() == before
    assert not list((wt / ".cw").glob("review-convergence/round-*-verdict.json"))
    assert not out_verdict.exists()


class TestUnreadFindingsBackstop:
    """R29 and Obs 2 at the CLI, whether or not the prose blocks first."""

    def _awaiting(self, wt: Path) -> bytes:
        ledger = ledger_with_rounds([], confirm="codex", shas=[_head(wt)])
        assert ledger.status == "awaiting_confirm"
        write_ledger(wt, ledger)
        return _ledger_path(wt).read_bytes()

    @pytest.mark.parametrize(
        ("name", "tail"),
        [("discarded", "SHOULD_FIX"), ("rejected", "MUST_FIX")],
    )
    def test_confirm_round_with_unread_findings_is_refused(
        self,
        runner: CliRunner,
        wt: Path,
        tmp_path: Path,
        name: str,
        tail: str,
    ) -> None:
        before = self._awaiting(wt)
        head = _head(wt)
        verdict = (
            consolidated_verdict(discarded={tail: 1}, sha=head)
            if name == "discarded"
            else consolidated_verdict(rejected_must_fix=True, sha=head)
        )
        out = tmp_path / "out-verdict.json"
        result = _record_verdict(
            runner,
            wt,
            _verdict_round("confirm", "codex", head, verdict),
            "--out-verdict",
            str(out),
        )
        assert result.exit_code == 1
        assert "Error: unread_findings:" in result.stderr
        assert isinstance(result.exception, SystemExit)
        _assert_nothing_recorded(wt, before, out)
        status = _out(_converge(runner, wt, "status"))
        assert status["status"] == "awaiting_confirm"
        assert status["next_action"] == "confirm"
        final = _converge(runner, wt, "finalize")
        assert final.exit_code == 1
        assert _out(final)["decision"] == "confirm_required"

    @pytest.mark.parametrize("name", ["discarded", "rejected"])
    def test_loop_round_under_opt_out_with_unread_findings_is_refused(
        self, runner: CliRunner, wt: Path, tmp_path: Path, name: str
    ) -> None:
        write_ledger(wt, fresh_ledger(confirm="none"))
        before = _ledger_path(wt).read_bytes()
        head = _head(wt)
        verdict = (
            consolidated_verdict(discarded={"SHOULD_FIX": 1}, sha=head)
            if name == "discarded"
            else consolidated_verdict(rejected_must_fix=True, sha=head)
        )
        out = tmp_path / "out-verdict.json"
        result = _record_verdict(
            runner,
            wt,
            _verdict_round("loop", LOOP_BACKEND, head, verdict),
            "--out-verdict",
            str(out),
        )
        assert result.exit_code == 1
        assert "unread_findings:" in result.stderr
        _assert_nothing_recorded(wt, before, out)

    def test_clean_verdicts_record_and_print_clean(
        self, runner: CliRunner, wt: Path
    ) -> None:
        self._awaiting(wt)
        head = _head(wt)
        result = _record_verdict(
            runner,
            wt,
            _verdict_round("confirm", "codex", head, consolidated_verdict(sha=head)),
        )
        assert result.exit_code == 0, result.stderr
        assert _out(result)["decision"] == "clean"

    def test_a_failed_role_is_never_recorded_clean_even_under_opt_out(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        write_ledger(wt, fresh_ledger(confirm="none"))
        before = _ledger_path(wt).read_bytes()
        head = _head(wt)
        role = "Code Quality Reviewer"
        out = tmp_path / "out-verdict.json"
        result = _record_verdict(
            runner,
            wt,
            _verdict_round(
                "loop",
                LOOP_BACKEND,
                head,
                consolidated_verdict(failed_role=role, sha=head),
            ),
            "--out-verdict",
            str(out),
        )
        assert result.exit_code == 1
        assert "failed_role:" in result.stderr
        assert role in result.stderr
        _assert_nothing_recorded(wt, before, out)
        assert _out(_converge(runner, wt, "status"))["next_action"] == "loop_round"
        final = _converge(runner, wt, "finalize")
        assert final.exit_code == 1
        assert _out(final)["decision"] == "confirm_required"

        clean = _record_verdict(
            runner,
            wt,
            _verdict_round("loop", LOOP_BACKEND, head, consolidated_verdict(sha=head)),
        )
        assert _out(clean)["decision"] == "clean"
        assert _converge(runner, wt, "finalize").exit_code == 0


class TestChainedAdjudicationKeepsTheDiscardFields:
    """Obs 1: the real commands between consolidate and record-round lose nothing."""

    _FAILED = "Code Quality Reviewer"

    def _consolidate_kwargs(self, case: str) -> dict[str, object]:
        if case == "discarded":
            failure = {
                "role": "Performance Reviewer",
                "reason": "codex_review_unparseable",
                "discarded_finding_count": 1,
                "discarded_finding_severities": {"SHOULD_FIX": 1},
            }
            return {"failed_reviewers": [failure]}
        if case == "rejected":
            doc = _make_reviewer_doc(
                _make_finding(
                    severity="MUST_FIX",
                    file="src/cw/never_in_the_diff.py",
                    line_start=None,
                    line_end=None,
                    summary="dropped before adjudication",
                ),
                reviewer_role="Security Reviewer",
            )
            return {"documents": [doc.model_dump(mode="json")]}
        if case == "failed_role":
            failure = {"role": self._FAILED, "reason": "unparseable_response"}
            return {"failed_reviewers": [failure]}
        return {"documents": [_make_reviewer_doc().model_dump(mode="json")]}

    @staticmethod
    def _lost(verdict: dict[str, Any]) -> tuple[object, object, object]:
        failed = [r for r in verdict["agents_run"] if r["status"] == "failed"]
        return (
            verdict["rejected_must_fix"],
            verdict["run_failures_with_should_fix_discards"],
            failed,
        )

    @pytest.mark.parametrize(
        ("case", "needle", "field"),
        [
            ("discarded", "unread_findings:", 1),
            ("rejected", "unread_findings:", 0),
            ("failed_role", "failed_role:", 2),
        ],
    )
    def test_the_guard_still_sees_what_consolidation_lost(
        self, runner: CliRunner, wt: Path, case: str, needle: str, field: int
    ) -> None:
        write_ledger(wt, fresh_ledger(confirm="none"))
        before = _ledger_path(wt).read_bytes()
        head = _head(wt)
        step = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(_consolidate_payload(**self._consolidate_kwargs(case))),
        )
        assert step.exit_code == 0, step.stderr
        verdict = json.loads(step.stdout)
        first = self._lost(verdict)
        assert first[field], f"consolidation produced no {case} record"

        voided = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps({"verdict": verdict, "ticket_id": TICKET_ID}),
        )
        assert voided.exit_code == 0, voided.stderr
        verdict = json.loads(voided.stdout)["verdict"]
        assert self._lost(verdict) == first

        suppressed = _converge(
            runner, wt, "suppress", "-", payload={"verdict": verdict}
        )
        assert suppressed.exit_code == 0, suppressed.stderr
        verdict = _out(suppressed)["verdict"]
        assert self._lost(verdict) == first

        adjudicated = runner.invoke(
            main,
            ["review", "adjudicate", "-"],
            input=json.dumps({"verdict": verdict, "adjudications": []}),
        )
        assert adjudicated.exit_code == 0, adjudicated.stderr
        verdict = json.loads(adjudicated.stdout)
        assert self._lost(verdict) == first

        recorded = _converge(
            runner,
            wt,
            "record-round",
            "-",
            payload={
                "kind": "loop",
                "backend": LOOP_BACKEND,
                "reviewed_sha": head,
                "roster": ROSTER,
                "verdict": verdict,
            },
        )
        assert recorded.exit_code == 1
        assert needle in recorded.stderr
        assert _ledger_path(wt).read_bytes() == before

    def test_a_control_with_no_lost_findings_records_clean(
        self, runner: CliRunner, wt: Path
    ) -> None:
        write_ledger(wt, fresh_ledger(confirm="none"))
        step = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(_consolidate_payload(**self._consolidate_kwargs("ok"))),
        )
        assert step.exit_code == 0, step.stderr
        verdict = json.loads(step.stdout)
        assert self._lost(verdict) == ([], [], [])
        recorded = _converge(
            runner,
            wt,
            "record-round",
            "-",
            payload={
                "kind": "loop",
                "backend": LOOP_BACKEND,
                "reviewed_sha": _head(wt),
                "roster": ROSTER,
                "verdict": verdict,
            },
        )
        assert recorded.exit_code == 0, recorded.stderr
        assert _out(recorded)["decision"] == "clean"


class TestRecordRoundRefusalsRecordNothing:
    """Any non-zero exit leaves the ledger and the verdict files untouched."""

    def _awaiting(self, wt: Path) -> bytes:
        write_ledger(wt, ledger_with_rounds([], confirm="codex", shas=[_head(wt)]))
        return _ledger_path(wt).read_bytes()

    def _assert_refused(
        self, result: Result, wt: Path, before: bytes, tmp_path: Path
    ) -> None:
        assert result.exit_code == 1
        assert result.stderr.startswith("Error: "), result.stderr
        assert isinstance(result.exception, SystemExit)
        _assert_nothing_recorded(wt, before, tmp_path / "never-written.json")

    def test_confirm_round_at_the_wrong_sha(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        before = self._awaiting(wt)
        result = _round(runner, wt, "confirm", "codex", "somewhere-else")
        self._assert_refused(result, wt, before, tmp_path)
        assert "awaiting-confirm candidate" in result.stderr

    def test_confirm_round_on_the_wrong_backend(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        before = self._awaiting(wt)
        result = _round(runner, wt, "confirm", "claude", _head(wt))
        self._assert_refused(result, wt, before, tmp_path)
        assert "confirm backend" in result.stderr

    def test_a_dropped_finding(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        write_ledger(wt, fresh_ledger(confirm="none"))
        before = _ledger_path(wt).read_bytes()
        head = _head(wt)
        undecided = stamp_dispositions(
            consolidated_verdict(accepted=[("MUST_FIX", "Undecided")], sha=head),
            {"Undecided": ("dropped", "")},
        )
        result = _record_verdict(
            runner, wt, _verdict_round("loop", LOOP_BACKEND, head, undecided)
        )
        self._assert_refused(result, wt, before, tmp_path)
        assert "undecided finding" in result.stderr

    def test_an_unreadable_ledger(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        path = _ledger_path(wt)
        path.parent.mkdir(parents=True)
        path.write_text('{"ticket_id": 1}', encoding="utf-8")
        before = path.read_bytes()
        result = _round(runner, wt, "loop", LOOP_BACKEND, _head(wt))
        self._assert_refused(result, wt, before, tmp_path)
        assert "cannot read the convergence ledger" in result.stderr

    def test_a_round_on_a_stopped_ledger(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        write_ledger(wt, stopped_ledger("no_progress", sha=_head(wt)))
        before = _ledger_path(wt).read_bytes()
        result = _round(runner, wt, "loop", LOOP_BACKEND, _head(wt))
        self._assert_refused(result, wt, before, tmp_path)
        assert "stopped ledger" in result.stderr


class TestConfirmFailed:
    """R8 and R26: the confirm backend being down never reads as converged."""

    def _awaiting(self, runner: CliRunner, wt: Path, ctx: Path) -> str:
        head = _head(wt)
        _init(runner, wt, ctx)
        loop = _out(_round(runner, wt, "loop", LOOP_BACKEND, head))
        assert loop["decision"] == "confirm_required"
        return head

    def _failed(
        self, runner: CliRunner, wt: Path, cause: str = "backend_error"
    ) -> dict[str, Any]:
        result = _converge(
            runner,
            wt,
            "confirm-failed",
            "--cause",
            cause,
            "--detail",
            "exit 1: capacity",
            "-c",
            _CLIENT,
        )
        assert result.exit_code == 0, result.stderr
        return _out(result)

    def test_backend_down_is_an_uncharged_blocked_not_converged(
        self,
        runner: CliRunner,
        wt: Path,
        ctx: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self._awaiting(runner, wt, ctx)
        out = self._failed(runner, wt)
        assert out["decision"] == "stop_confirm_unavailable"
        assert out["consecutive"] == 1
        blocker = out["blocker"]
        assert blocker["reason"] == "confirm_backend_unavailable"
        assert (
            f"cw dev-queue requeue {TICKET_ID} -c {_CLIENT}" in blocker["recovery_hint"]
        )
        with caplog.at_level("WARNING", logger="cw.auto_dev_result"):
            result = _blocked_result(blocker, out["review"])
        assert result.review.converged is False
        assert "blocker_reason_unknown" not in caplog.text
        final = _converge(runner, wt, "finalize")
        assert final.exit_code == 1
        assert _out(final)["decision"] == "confirm_required"
        assert "sentinel_review" not in _out(final)

    def test_the_fourth_same_cause_failure_escalates_across_sessions(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        head = self._awaiting(runner, wt, ctx)
        for expected in (1, 2, 3):
            # A fresh runner per call: a simulated session boundary.
            out = self._failed(CliRunner(), wt)
            assert out["decision"] == "stop_confirm_unavailable"
            assert out["consecutive"] == expected
        out = self._failed(CliRunner(), wt)
        assert out["decision"] == "stop_confirm_escalated"
        blocker = out["blocker"]
        assert blocker["reason"] == "agent_block"
        assert blocker["stage"] == "stage3_review"
        assert "backend_error" in blocker["details"]
        assert head[:7] in blocker["details"]
        assert "4" in blocker["details"]
        assert "cw dev-queue requeue" in blocker["recovery_hint"]
        assert "claim evidence" in blocker["recovery_hint"]
        assert not re.search(r"(?<!un)charged", blocker["recovery_hint"])
        result = _blocked_result(blocker, out["review"])
        assert result.review.converged is False

    def test_a_recorded_round_resets_the_count(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        head = self._awaiting(runner, wt, ctx)
        assert self._failed(runner, wt)["consecutive"] == 1
        assert self._failed(runner, wt)["consecutive"] == 2
        dirty = _round(runner, wt, "confirm", "codex", head, *_dirty(1, "c"))
        assert _out(dirty)["decision"] == "continue"
        again = _out(_round(runner, wt, "loop", LOOP_BACKEND, head))
        assert again["decision"] == "confirm_required"
        assert self._failed(runner, wt)["consecutive"] == 1

    def test_another_cause_resets_the_count(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        self._awaiting(runner, wt, ctx)
        self._failed(runner, wt)
        self._failed(runner, wt)
        other = self._failed(runner, wt, cause="crash")
        assert other["decision"] == "stop_confirm_unavailable"
        assert other["consecutive"] == 1

    def test_without_an_awaiting_confirm_ledger_it_is_a_one_line_error(
        self, runner: CliRunner, wt: Path
    ) -> None:
        write_ledger(wt, fresh_ledger())
        result = _converge(runner, wt, "confirm-failed", "--cause", "crash")
        assert result.exit_code == 1
        assert "awaiting_confirm" in result.stderr
        assert isinstance(result.exception, SystemExit)

    def test_the_client_option_is_optional(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        self._awaiting(runner, wt, ctx)
        result = _converge(runner, wt, "confirm-failed", "--cause", "crash")
        assert result.exit_code == 0, result.stderr
        hint = _out(result)["blocker"]["recovery_hint"]
        assert f"cw dev-queue requeue {TICKET_ID} -c <C>" in hint


class TestHistoryComment:
    def test_writes_the_comment_when_rounds_exist(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        write_ledger(wt, ledger_with_rounds(_dirty(2, "x"), shas=[_head(wt)]))
        out = tmp_path / "nested" / "history.md"
        result = _converge(runner, wt, "history-comment", "--out", str(out))
        assert result.exit_code == 0, result.stderr
        assert _out(result) == {"written": True, "out": str(out)}
        assert out.read_text(encoding="utf-8").startswith(
            "## Review Convergence History"
        )

    def test_a_stopped_ledger_names_the_stop_and_the_settle_payload(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        write_ledger(
            wt,
            stopped_ledger("oscillation", sha=_head(wt), stop_identities=(_RECURRING,)),
        )
        out = tmp_path / "history.md"
        _converge(runner, wt, "history-comment", "--out", str(out))
        text = out.read_text(encoding="utf-8")
        assert "### Stopped: oscillation" in text
        assert "operator-settle" in text
        assert _RECURRING in text

    @pytest.mark.parametrize("seed", ["no_ledger", "no_rounds"])
    def test_writes_nothing_without_rounds(
        self, runner: CliRunner, wt: Path, tmp_path: Path, seed: str
    ) -> None:
        if seed == "no_rounds":
            write_ledger(wt, fresh_ledger())
        out = tmp_path / "history.md"
        result = _converge(runner, wt, "history-comment", "--out", str(out))
        assert result.exit_code == 0, result.stderr
        assert _out(result)["written"] is False
        assert not out.exists()


def _pushed_and_converged(wt: Path) -> bytes:
    """A clean confirmed round at HEAD (already pushed); returns the ledger bytes."""
    write_ledger(wt, confirmed_ledger(sha=_head(wt)))
    return _ledger_path(wt).read_bytes()


def _cw_files(wt: Path) -> list[str]:
    return sorted(str(p.relative_to(wt)) for p in (wt / ".cw").rglob("*"))


class _GitSpy:
    """Records ``run_git`` calls of the tip step and delegates to the real one."""

    def __init__(self, real: Callable[..., Any]) -> None:
        self.real = real
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append((list(argv), str(kwargs.get("cwd"))))
        return self.real(argv, **kwargs)


@pytest.fixture
def git_spy(monkeypatch: pytest.MonkeyPatch) -> _GitSpy:
    spy = _GitSpy(run_git)
    monkeypatch.setattr("cw.cli.review.converge_tip.run_git", spy)
    return spy


class TestTipCommands:
    """``verify-tip`` and ``finalize``: the strict, fetching final-sha check."""

    @pytest.mark.parametrize("sub", ["verify-tip", "finalize"])
    def test_accepts_a_clean_confirmed_tip_that_is_pushed(
        self, runner: CliRunner, wt: Path, sub: str
    ) -> None:
        before = _pushed_and_converged(wt)
        result = _converge(runner, wt, sub)
        assert result.exit_code == 0, result.stdout + result.stderr
        out = _out(result)
        assert out["decision"] == "accepted"
        assert out["reason"] is None
        assert _ledger_path(wt).read_bytes() == before
        if sub == "finalize":
            assert out["final_round_clean_sha"] == _head(wt)
            assert out["sentinel_review"]["converged"] is True
            assert out["sentinel_review"]["final_round_clean_sha"] == _head(wt)
            assert any(
                line.startswith("review_round_1:") for line in out["friction_lines"]
            )
        else:
            assert "sentinel_review" not in out

    @pytest.mark.parametrize("sub", ["verify-tip", "finalize"])
    def test_a_custom_ledger_path_is_read_relative_to_the_worktree(
        self, runner: CliRunner, wt: Path, sub: str
    ) -> None:
        """prep-pr passes ``--ledger .cw/prep-pr-convergence.json``."""
        write_ledger(wt, confirmed_ledger(sha=_head(wt)))
        shutil.copy(_ledger_path(wt), wt / ".cw" / "prep-pr-convergence.json")
        _ledger_path(wt).unlink()
        result = runner.invoke(
            main,
            [
                "review",
                "converge",
                sub,
                "--worktree",
                str(wt),
                "--ledger",
                ".cw/prep-pr-convergence.json",
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("sub", ["verify-tip", "finalize"])
    def test_default_branch_is_not_an_option(
        self, runner: CliRunner, wt: Path, sub: str
    ) -> None:
        result = _converge(runner, wt, sub, "--default-branch", "main")
        assert result.exit_code == 2
        assert "No such option" in result.stderr

    @pytest.mark.parametrize("sub", ["verify-tip", "finalize"])
    def test_a_missing_ledger_needs_a_review_round(
        self, runner: CliRunner, wt: Path, git_spy: _GitSpy, sub: str
    ) -> None:
        result = _converge(runner, wt, sub)
        assert result.exit_code == 1
        out = _out(result)
        assert out == {"decision": "rereview_required", "reason": "no_clean_round"}
        assert git_spy.calls == []

    def test_verify_tip_without_a_clean_round_makes_no_network_call(
        self, runner: CliRunner, wt: Path, git_spy: _GitSpy
    ) -> None:
        write_ledger(wt, ledger_with_rounds(_dirty(1, "x"), shas=[_head(wt)]))
        result = _converge(runner, wt, "verify-tip")
        assert result.exit_code == 1
        assert _out(result)["reason"] == "no_clean_round"
        assert git_spy.calls == []

    def test_finalize_on_an_unconverged_ledger_wants_the_confirm_round(
        self, runner: CliRunner, wt: Path, git_spy: _GitSpy
    ) -> None:
        write_ledger(wt, ledger_with_rounds([], confirm="codex", shas=[_head(wt)]))
        before = _ledger_path(wt).read_bytes()
        result = _converge(runner, wt, "finalize")
        assert result.exit_code == 1
        assert _out(result)["decision"] == "confirm_required"
        assert _ledger_path(wt).read_bytes() == before
        assert git_spy.calls == []

    def test_a_merge_of_origin_default_needs_another_round(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
    ) -> None:
        """R22: any commit after the last clean round, a merge included."""
        wt, origin = wt_origin
        before = _pushed_and_converged(wt)
        push_commit_to_origin(origin, "main", tmp_path / "side", "main-moved.py")
        git_in(wt, "fetch", "origin")
        git_in(wt, "merge", "--no-edit", "origin/main")
        git_in(wt, "push", "origin", "feature")
        verify = _converge(runner, wt, "verify-tip")
        assert verify.exit_code == 1
        assert _out(verify) == {
            "decision": "rereview_required",
            "reason": "commits_after_clean_round",
        }
        assert _ledger_path(wt).read_bytes() == before

        final = _converge(runner, wt, "finalize")
        assert final.exit_code == 1
        assert _out(final)["reason"] == "commits_after_clean_round"
        assert "sentinel_review" not in _out(final)
        demoted = _stored(wt)
        assert demoted.status == "in_progress"
        assert demoted.last_clean_sha is not None

    def test_a_stale_remote_tracking_ref_never_false_accepts(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
    ) -> None:
        wt, origin = wt_origin
        before = _pushed_and_converged(wt)
        assert _converge(runner, wt, "verify-tip").exit_code == 0
        assert _converge(runner, wt, "finalize").exit_code == 0
        stale = git_in(wt, "rev-parse", "refs/remotes/origin/feature")
        assert stale == _head(wt)

        new_tip = push_commit_to_origin(
            origin, "feature", tmp_path / "side", "elsewhere.py"
        )
        # The worktree still believes origin/feature is HEAD.
        assert git_in(wt, "rev-parse", "refs/remotes/origin/feature") == stale

        for sub in ("verify-tip", "finalize"):
            result = _converge(runner, wt, sub)
            assert result.exit_code == 1, sub
            out = _out(result)
            assert out["decision"] == "rereview_required"
            assert out["reason"] == "head_not_origin_tip"
            assert "sentinel_review" not in out
            assert _ledger_path(wt).read_bytes() == before
        # The by-name fetch is what moved the tracking ref.
        assert git_in(wt, "rev-parse", "refs/remotes/origin/feature") == new_tip

    def test_the_control_without_the_extra_push_leaves_the_ref_alone(
        self, runner: CliRunner, wt: Path
    ) -> None:
        _pushed_and_converged(wt)
        ref = git_in(wt, "rev-parse", "refs/remotes/origin/feature")
        assert _converge(runner, wt, "finalize").exit_code == 0
        assert git_in(wt, "rev-parse", "refs/remotes/origin/feature") == ref

    @pytest.mark.parametrize("explicit", [False, True], ids=["default", "--branch"])
    def test_the_first_git_call_is_the_by_name_fetch(
        self, runner: CliRunner, wt: Path, git_spy: _GitSpy, explicit: bool
    ) -> None:
        _pushed_and_converged(wt)
        extra = ("--branch", "feature") if explicit else ()
        for sub in ("verify-tip", "finalize"):
            git_spy.calls.clear()
            assert _converge(runner, wt, sub, *extra).exit_code == 0
            assert git_spy.calls[0] == (["fetch", "origin", "feature"], str(wt))

    @pytest.mark.parametrize("variant", ["origin_deleted", "origin_repointed"])
    @pytest.mark.parametrize("sub", ["verify-tip", "finalize"])
    def test_a_failing_fetch_fails_closed_and_writes_nothing(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        variant: str,
        sub: str,
    ) -> None:
        wt, origin = wt_origin
        before = _pushed_and_converged(wt)
        files = _cw_files(wt)
        if variant == "origin_deleted":
            shutil.rmtree(origin)
        else:
            git_in(wt, "remote", "set-url", "origin", str(tmp_path / "no-such-repo"))
        result = _converge(runner, wt, sub)
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        out = _out(result)
        assert out["decision"] == "rereview_required"
        assert out["reason"] == "head_not_origin_tip"
        assert "git fetch origin feature failed" in out["detail"]
        assert "sentinel_review" not in out
        assert _ledger_path(wt).read_bytes() == before
        assert _cw_files(wt) == files

    @pytest.mark.parametrize("sub", ["verify-tip", "finalize"])
    def test_git_that_cannot_run_fails_closed(
        self,
        runner: CliRunner,
        wt: Path,
        monkeypatch: pytest.MonkeyPatch,
        sub: str,
    ) -> None:
        before = _pushed_and_converged(wt)

        monkeypatch.setattr(
            "cw.cli.review.converge_tip.run_git", _raiser(OSError("git vanished"))
        )
        result = _converge(runner, wt, sub)
        assert result.exit_code == 1
        out = _out(result)
        assert out["reason"] == "head_not_origin_tip"
        assert "git vanished" in out["detail"]
        assert _ledger_path(wt).read_bytes() == before

    def test_an_origin_ref_that_does_not_resolve_fails_closed(
        self, runner: CliRunner, wt: Path
    ) -> None:
        """A branch that was never pushed: the fetch itself fails by name."""
        git_in(wt, "checkout", "-b", "never-pushed")
        write_ledger(wt, confirmed_ledger(sha=_head(wt)))
        before = _ledger_path(wt).read_bytes()
        result = _converge(runner, wt, "finalize")
        assert result.exit_code == 1
        out = _out(result)
        assert out["reason"] == "head_not_origin_tip"
        assert "git fetch origin never-pushed failed" in out["detail"]
        assert _ledger_path(wt).read_bytes() == before

    @pytest.mark.parametrize(
        ("failing", "needle"),
        [("HEAD", "cannot read HEAD"), ("origin/feature", "does not resolve")],
    )
    def test_an_unreadable_tip_after_the_fetch_fails_closed(
        self,
        runner: CliRunner,
        wt: Path,
        monkeypatch: pytest.MonkeyPatch,
        failing: str,
        needle: str,
    ) -> None:
        before = _pushed_and_converged(wt)

        boom = _raiser(OSError("boom"))

        def _flaky(argv: list[str], **kwargs: Any) -> Any:
            if argv == ["rev-parse", failing]:
                if failing == "HEAD":
                    return boom()
                return subprocess.CompletedProcess(argv, 128, "", "fatal: bad ref")
            return run_git(argv, **kwargs)

        monkeypatch.setattr("cw.cli.review.converge_tip.run_git", _flaky)
        result = _converge(runner, wt, "verify-tip")
        assert result.exit_code == 1
        assert needle in _out(result)["detail"]
        assert _ledger_path(wt).read_bytes() == before

    def test_push_after_a_behind_head_failure_succeeds_without_a_round(
        self, runner: CliRunner, wt: Path
    ) -> None:
        """Option A: ``head_not_origin_tip`` never demotes, so the re-run is clean."""
        commit_tracked_file(wt, "s1.py")
        write_ledger(wt, confirmed_ledger(sha=_head(wt)))
        before = _ledger_path(wt).read_bytes()

        first = _converge(runner, wt, "finalize")
        assert first.exit_code == 1
        out = _out(first)
        assert out["reason"] == "head_not_origin_tip"
        assert "sentinel_review" not in out
        assert _ledger_path(wt).read_bytes() == before
        mirror = _converge(runner, wt, "verify-tip")
        assert mirror.exit_code == 1
        assert _ledger_path(wt).read_bytes() == before

        git_in(wt, "push", "origin", "feature")
        again = _converge(runner, wt, "finalize")
        assert again.exit_code == 0, again.stdout + again.stderr
        assert "sentinel_review" in _out(again)
        assert _stored(wt).status == "converged"
        assert _converge(runner, wt, "verify-tip").exit_code == 0

    def test_a_pushed_commit_after_the_failure_demotes(
        self, runner: CliRunner, wt: Path
    ) -> None:
        commit_tracked_file(wt, "s1.py")
        write_ledger(wt, confirmed_ledger(sha=_head(wt)))
        assert _converge(runner, wt, "finalize").exit_code == 1
        commit_tracked_file(wt, "s2.py")
        git_in(wt, "push", "origin", "feature")
        result = _converge(runner, wt, "finalize")
        assert result.exit_code == 1
        assert _out(result)["reason"] == "commits_after_clean_round"
        assert _stored(wt).status == "in_progress"

    def test_an_unpushed_commit_after_the_failure_still_reports_the_push(
        self, runner: CliRunner, wt: Path
    ) -> None:
        commit_tracked_file(wt, "s1.py")
        write_ledger(wt, confirmed_ledger(sha=_head(wt)))
        assert _converge(runner, wt, "finalize").exit_code == 1
        commit_tracked_file(wt, "s2.py")
        result = _converge(runner, wt, "finalize")
        assert result.exit_code == 1
        assert _out(result)["reason"] == "head_not_origin_tip"
        assert _stored(wt).status == "converged"

    def test_a_detached_head_needs_an_explicit_branch(
        self, runner: CliRunner, wt: Path
    ) -> None:
        _pushed_and_converged(wt)
        git_in(wt, "checkout", "--detach")
        result = _converge(runner, wt, "verify-tip")
        assert result.exit_code == 1
        assert "detached HEAD" in result.stderr
        assert isinstance(result.exception, SystemExit)
        assert _converge(runner, wt, "verify-tip", "--branch", "feature").exit_code == 0

    def test_a_worktree_that_is_not_a_repository_is_a_one_line_error(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        plain = tmp_path / "plain"
        write_ledger(plain, confirmed_ledger(sha="a" * 40))
        result = _converge(runner, plain, "verify-tip")
        assert result.exit_code == 1
        assert "cannot read the current branch" in result.stderr

    def test_an_unreadable_ledger_is_a_one_line_error(
        self, runner: CliRunner, wt: Path
    ) -> None:
        path = _ledger_path(wt)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xfe")
        result = _converge(runner, wt, "verify-tip")
        assert result.exit_code == 1
        assert "cannot read the convergence ledger" in result.stderr
        assert result.stdout == ""


def _settle_events() -> list[Any]:
    return read_events(event_types=[OrchestratorEventType.REVIEW_FINDING_SETTLED])


def _targets(
    *summaries: str, file: str = "src/cw/foo.py", severity: str = "MUST_FIX"
) -> dict[str, object]:
    """An ``operator-settle`` payload over *summaries*."""
    return {
        "entries": [
            {"severity": severity, "file": file, "summary": summary}
            for summary in summaries
        ]
    }


def _park(
    wt: Path | None, *, status: QueueItemStatus = QueueItemStatus.BLOCKED_ON_USER
) -> TicketTask:
    return _seed_review_row(TICKET_ID, _CLIENT, status=status, worktree_path=wt)


def _stage_oscillation(
    wt: Path,
    *,
    status: QueueItemStatus = QueueItemStatus.BLOCKED_ON_USER,
    row_worktree: Path | None = None,
    row_has_worktree: bool = True,
) -> bytes:
    """A dev-queue row plus an oscillation-stopped ledger in *wt*; ledger bytes.

    The row's worktree is *wt* unless *row_worktree* names another, or
    *row_has_worktree* is false (no ``worktree_path`` at all). ``add_ticket``
    declines a second live row for the ticket, so the row is seeded once, here.
    """
    if row_has_worktree:
        _park(row_worktree or wt, status=status)
    else:
        _park(None, status=status)
    write_ledger(
        wt,
        stopped_ledger("oscillation", sha=_head(wt), stop_identities=(_RECURRING,)),
    )
    return _ledger_path(wt).read_bytes()


def _operator_settle(
    runner: CliRunner,
    payload: object,
    *extra: str,
    reason: str = _SETTLE_REASON,
) -> Result:
    return runner.invoke(
        main,
        [
            "review",
            "converge",
            "operator-settle",
            "-",
            "--ticket-id",
            TICKET_ID,
            "-c",
            _CLIENT,
            "--reason",
            reason,
            *extra,
        ],
        input=json.dumps(payload),
    )


class TestOperatorSettle:
    """R12 and R21: the operator's false-positive ingress."""

    @freeze_time("2026-10-27 12:00:00")
    def test_settle_then_requeue_releases_an_oscillation_stop(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        head = _head(wt)
        _stage_oscillation(wt)
        # The mirror: without a settle the same-tip requeue re-emits the stop.
        stuck = _out(_init(runner, wt, ctx))
        assert stuck["reemitted"] is True
        assert stuck["stop"]["releasable"] is False

        result = _operator_settle(runner, _targets(_RECURRING))
        assert result.exit_code == 0, result.stdout + result.stderr
        out = _out(result)
        assert out["unmatched"] == []
        assert out["releasable"] is True
        assert out["next"] == f"cw dev-queue requeue {TICKET_ID} -c {_CLIENT}"
        assert len(out["settled"]) == 1

        entry = _stored(wt).settled[-1]
        assert entry.origin == "operator"
        assert entry.actor == _OPERATOR
        assert entry.settled_at == "2026-10-27T12:00:00Z"
        assert entry.rationale == _SETTLE_REASON
        events = _settle_events()
        assert len(events) == 1
        assert events[0].correlation_id == TICKET_ID
        assert events[0].payload == {
            "key": f"MUST_FIX::src/cw/foo.py::{_RECURRING}",
            "file": "src/cw/foo.py",
            "summary": _RECURRING,
            "outcome": "REJECTED",
            "reason": _SETTLE_REASON,
            "actor": _OPERATOR,
            "recorded_at": "2026-10-27T12:00:00Z",
            "reviewed_sha": head,
        }

        released = _out(_init(runner, wt, ctx))
        assert released["released"]["cause"] == "identities_settled"
        recurrence = _round(
            runner, wt, "loop", LOOP_BACKEND, head, round_finding(summary=_RECURRING)
        )
        assert recurrence.exit_code == 0, recurrence.stderr
        assert _out(recurrence)["decision"] != "stop_not_converging"
        assert _stored(wt).status != "stopped"

    def test_an_explicit_ledger_path_needs_no_worktree_on_the_row(
        self, runner: CliRunner, wt: Path
    ) -> None:
        _stage_oscillation(wt, row_has_worktree=False)
        result = _operator_settle(
            runner, _targets(_RECURRING), "--ledger", str(_ledger_path(wt))
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert _stored(wt).settled[-1].origin == "operator"

    def test_an_operator_action_stop_with_an_na_finding_end_to_end(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        head = _head(wt)
        finding = operator_finding()
        _park(wt)
        _init(runner, wt, ctx)
        stop = _round(runner, wt, "loop", LOOP_BACKEND, head, finding)
        assert _out(stop)["decision"] == "stop_operator_action"
        status = _out(_converge(runner, wt, "status"))
        assert status["stop"]["identities"][0]["file"] == "N/A"
        payload = status["stop"]["settle_payload"]
        assert payload["entries"][0]["file"] == "N/A"

        # The control: requeue releases the checklist, the finding re-raises.
        released = _out(_init(runner, wt, ctx))
        assert released["released"]["cause"] == "operator_action_done"
        again = _round(runner, wt, "loop", LOOP_BACKEND, head, finding)
        assert _out(again)["decision"] == "stop_operator_action"

        result = _operator_settle(runner, payload)
        assert result.exit_code == 0, result.stdout + result.stderr
        out = _out(result)
        assert len(out["settled"]) == 1
        assert out["unmatched"] == []
        assert out["releasable"] is True
        events = _settle_events()
        assert len(events) == 1
        assert events[0].payload["file"] == "N/A"
        assert events[0].payload["key"] == "MUST_FIX::N/A::Operator must do X"
        assert events[0].payload["actor"] == _OPERATOR
        assert events[0].payload["reason"] == _SETTLE_REASON
        assert events[0].payload["reviewed_sha"] == head

        released = _out(_init(runner, wt, ctx))
        assert released["released"]["cause"] == "operator_action_done"
        after = _round(runner, wt, "loop", LOOP_BACKEND, head, finding)
        assert _out(after)["decision"] == "confirm_required"

    def test_cw_review_settle_still_rejects_an_na_finding(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            main,
            [
                "review",
                "settle",
                "--reason",
                _SETTLE_REASON,
                "--ticket",
                TICKET_ID,
                "--out",
                str(tmp_path / "out.md"),
                "-",
            ],
            input=json.dumps(_settle_payload(_settle_entry(file="N/A"))),
        )
        assert result.exit_code == 1
        assert "N/A" in result.stderr

    def test_severity_keys_the_settle(self, runner: CliRunner, wt: Path) -> None:
        _park(wt)
        write_ledger(
            wt,
            ledger_with_rounds(
                [
                    round_finding("MUST_FIX", "Dup"),
                    round_finding("SHOULD_FIX", "Dup"),
                ],
                shas=[_head(wt)],
            ),
        )
        both = {
            "entries": [
                {"severity": "MUST_FIX", "file": "src/cw/foo.py", "summary": "Dup"},
                {"severity": "SHOULD_FIX", "file": "src/cw/foo.py", "summary": "Dup"},
            ]
        }
        result = _operator_settle(runner, both)
        assert result.exit_code == 0, result.stdout + result.stderr
        assert len(_out(result)["settled"]) == 2
        assert len(_stored(wt).settled) == 2
        keys = {e.payload["key"] for e in _settle_events()}
        assert keys == {
            "MUST_FIX::src/cw/foo.py::Dup",
            "SHOULD_FIX::src/cw/foo.py::Dup",
        }

    def test_a_repeated_identity_records_one_entry_and_one_event(
        self, runner: CliRunner, wt: Path
    ) -> None:
        _stage_oscillation(wt)
        result = _operator_settle(
            runner, _targets(_RECURRING, _RECURRING, f"  {_RECURRING}  ")
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert len(_out(result)["settled"]) == 1
        assert len([e for e in _stored(wt).settled if e.origin == "operator"]) == 1
        assert len(_settle_events()) == 1


class TestOperatorSettleRefusals:
    """Every refusal writes nothing: no ledger change and no event."""

    def _assert_untouched(self, wt: Path, before: bytes) -> None:
        assert _ledger_path(wt).read_bytes() == before
        assert _settle_events() == []

    def test_inside_a_headless_worker(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        before = _stage_oscillation(wt)
        _write_session_context(tmp_path, headless=True)
        result = _operator_settle(runner, _targets(_RECURRING))
        assert result.exit_code == 1
        assert "Refusing to settle" in result.stderr
        self._assert_untouched(wt, before)

    @pytest.mark.parametrize("reason", ["", "   "])
    def test_a_blank_reason(self, runner: CliRunner, wt: Path, reason: str) -> None:
        before = _stage_oscillation(wt)
        result = _operator_settle(runner, _targets(_RECURRING), reason=reason)
        assert result.exit_code == 1
        assert "--reason" in result.stderr
        self._assert_untouched(wt, before)

    def test_an_unresolvable_gh_identity(
        self, runner: CliRunner, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = _stage_oscillation(wt)
        monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: None)
        result = _operator_settle(runner, _targets(_RECURRING))
        assert result.exit_code == 1
        assert "GitHub identity" in result.stderr
        self._assert_untouched(wt, before)

    def test_a_running_row(self, runner: CliRunner, wt: Path) -> None:
        before = _stage_oscillation(wt, status=QueueItemStatus.RUNNING)
        row = find_ticket_row(TICKET_ID, _CLIENT)
        assert row is not None
        assert row.status is QueueItemStatus.RUNNING
        result = _operator_settle(runner, _targets(_RECURRING))
        assert result.exit_code == 1
        assert "RUNNING" in result.stderr
        self._assert_untouched(wt, before)

    def test_a_row_with_no_worktree_path(self, runner: CliRunner, wt: Path) -> None:
        before = _stage_oscillation(wt, row_has_worktree=False)
        result = _operator_settle(runner, _targets(_RECURRING))
        assert result.exit_code == 1
        assert "no worktree_path" in result.stderr
        self._assert_untouched(wt, before)

    def test_a_worktree_with_no_ledger(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        before = _stage_oscillation(wt, row_worktree=tmp_path / "empty-worktree")
        result = _operator_settle(runner, _targets(_RECURRING))
        assert result.exit_code == 1
        assert "no convergence ledger" in result.stderr
        self._assert_untouched(wt, before)

    def test_a_missing_dev_queue_row(self, runner: CliRunner, wt: Path) -> None:
        write_ledger(wt, fresh_ledger())
        result = _operator_settle(runner, _targets(_RECURRING))
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)

    @pytest.mark.parametrize(
        ("target", "has_nearest"),
        [
            (_targets("Recurring defects"), True),
            (
                _targets(
                    "Something else entirely unrelated",
                    file="docs/other.md",
                    severity="SHOULD_FIX",
                ),
                False,
            ),
        ],
        ids=["mis-copied-anchor", "never-seen"],
    )
    def test_an_unmatched_entry_prints_the_json_and_writes_nothing(
        self,
        runner: CliRunner,
        wt: Path,
        target: dict[str, object],
        has_nearest: bool,
    ) -> None:
        before = _stage_oscillation(wt)
        result = _operator_settle(runner, target)
        assert result.exit_code == 1
        out = _out(result)
        assert out["written"] is False
        assert out["settled"] == []
        assert len(out["unmatched"]) == 1
        nearest = out["unmatched"][0]["nearest"]
        if has_nearest:
            assert nearest["summary_normalized"] == _RECURRING
        else:
            assert nearest is None
        assert "settle_payload" in out["hint"]
        self._assert_untouched(wt, before)

    def test_a_mixed_payload_is_all_or_nothing(
        self, runner: CliRunner, wt: Path
    ) -> None:
        before = _stage_oscillation(wt)
        result = _operator_settle(runner, _targets(_RECURRING, "Never recorded"))
        assert result.exit_code == 1
        assert len(_out(result)["unmatched"]) == 1
        self._assert_untouched(wt, before)

    def test_allow_unmatched_is_not_an_option(
        self, runner: CliRunner, wt: Path
    ) -> None:
        before = _stage_oscillation(wt)
        result = _operator_settle(
            runner, _targets("Never recorded"), "--allow-unmatched"
        )
        assert result.exit_code == 2
        helped = runner.invoke(
            main, ["review", "converge", "operator-settle", "--help"]
        )
        assert "--allow-unmatched" not in helped.stdout
        self._assert_untouched(wt, before)

    def test_an_event_store_failure_keeps_the_settle_and_a_rerun_records_the_event(
        self, runner: CliRunner, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = _stage_oscillation(wt)

        with monkeypatch.context() as outage:
            outage.setattr(
                "cw.events.record_event", _raiser(OSError("No space left on device"))
            )
            failed = _operator_settle(runner, _targets(_RECURRING))
        assert failed.exit_code == 1
        assert "The ledger settle was saved" in failed.stderr
        assert "No space left on device" in failed.stderr
        assert _ledger_path(wt).read_bytes() != before
        assert len(_stored(wt).settled) == 1
        assert _settle_events() == []

        rerun = _operator_settle(runner, _targets(_RECURRING))
        assert rerun.exit_code == 0, rerun.stdout + rerun.stderr
        assert len(_stored(wt).settled) == 1
        assert len(_settle_events()) == 1

    def test_a_ledger_for_another_ticket_via_the_row(
        self, runner: CliRunner, wt: Path
    ) -> None:
        _park(wt)
        write_ledger(
            wt,
            ledger_with_rounds(_dirty(1, "x"), shas=[_head(wt)], ticket_id="T-OTHER"),
        )
        before = _ledger_path(wt).read_bytes()
        result = _operator_settle(runner, _targets("x 0"))
        assert result.exit_code == 1
        assert "belongs to ticket T-OTHER" in result.stderr
        self._assert_untouched(wt, before)

    def test_a_ledger_for_another_ticket_via_an_explicit_path(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        before = _stage_oscillation(wt)
        other = tmp_path / "other"
        write_ledger(
            other,
            ledger_with_rounds(_dirty(1, "x"), shas=["s"], ticket_id="T-OTHER"),
        )
        other_before = _ledger_path(other).read_bytes()
        result = _operator_settle(
            runner, _targets("x 0"), "--ledger", str(_ledger_path(other))
        )
        assert result.exit_code == 1
        assert "belongs to ticket T-OTHER" in result.stderr
        assert _ledger_path(other).read_bytes() == other_before
        self._assert_untouched(wt, before)

    @pytest.mark.parametrize("content", ["{not json", '{"ticket_id": 1}'])
    def test_an_unreadable_ledger_is_a_one_line_error(
        self, runner: CliRunner, wt: Path, content: str
    ) -> None:
        _park(wt)
        path = _ledger_path(wt)
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")
        result = _operator_settle(runner, _targets(_RECURRING))
        assert result.exit_code == 1
        assert "cannot read the convergence ledger" in result.stderr
        assert "Traceback" not in result.output
        assert isinstance(result.exception, SystemExit)
        assert path.read_text(encoding="utf-8") == content
        assert _settle_events() == []

    def test_a_value_error_from_the_engine_is_a_one_line_error(
        self, runner: CliRunner, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = _stage_oscillation(wt)

        monkeypatch.setattr(
            "cw.cli.review.operator_settle.operator_settle",
            _raiser(ValueError("engine says no")),
        )
        result = _operator_settle(runner, _targets(_RECURRING))
        assert result.exit_code == 1
        assert "engine says no" in result.stderr
        assert isinstance(result.exception, SystemExit)
        self._assert_untouched(wt, before)

    def test_a_ledger_with_no_rounds(self, runner: CliRunner, wt: Path) -> None:
        _park(wt)
        write_ledger(wt, fresh_ledger())
        before = _ledger_path(wt).read_bytes()
        result = _operator_settle(runner, _targets(_RECURRING))
        assert result.exit_code == 1
        assert "no recorded rounds" in result.stderr
        self._assert_untouched(wt, before)

    @pytest.mark.parametrize("payload", [{"entries": []}, {"entries": [{"file": "x"}]}])
    def test_an_invalid_payload(
        self, runner: CliRunner, wt: Path, payload: dict[str, object]
    ) -> None:
        before = _stage_oscillation(wt)
        result = _operator_settle(runner, payload)
        assert result.exit_code == 1
        assert "entries" in result.stderr
        self._assert_untouched(wt, before)


class _RowSequence:
    """A ``_find_ticket`` stand-in returning *rows* in call order (the last repeats)."""

    def __init__(self, *rows: Any) -> None:
        self.rows = rows
        self.calls = 0

    def __call__(self, *_args: object, **_kwargs: object) -> Any:
        row = self.rows[min(self.calls, len(self.rows) - 1)]
        self.calls += 1
        return row


class TestOperatorSettleRace:
    """Obs 3: a worker that starts between the status check and the write."""

    def test_a_row_that_started_running_is_reported(
        self, runner: CliRunner, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parked = _park(wt)
        _stage_oscillation(wt)
        running = parked.model_copy(update={"status": QueueItemStatus.RUNNING})
        spy = _RowSequence(parked, running)
        monkeypatch.setattr("cw.cli.review.operator_settle._find_ticket", spy)
        result = _operator_settle(runner, _targets(_RECURRING))
        assert result.exit_code == 1
        assert spy.calls == 2
        assert "re-run the same operator-settle" in result.stderr
        out = _out(result)
        assert out["warning"] == "row_started_running"
        # The settle was written; the warning is that one write may have been lost.
        assert [e for e in _stored(wt).settled if e.origin == "operator"]

    def test_the_rerun_against_a_parked_row_is_idempotent(
        self, runner: CliRunner, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parked = _park(wt)
        _stage_oscillation(wt)
        running = parked.model_copy(update={"status": QueueItemStatus.RUNNING})
        monkeypatch.setattr(
            "cw.cli.review.operator_settle._find_ticket", _RowSequence(parked, running)
        )
        assert _operator_settle(runner, _targets(_RECURRING)).exit_code == 1

        monkeypatch.setattr(
            "cw.cli.review.operator_settle._find_ticket", _RowSequence(parked)
        )
        rerun = _operator_settle(runner, _targets(_RECURRING))
        assert rerun.exit_code == 0, rerun.stdout + rerun.stderr
        assert "warning" not in _out(rerun)
        operator = [e for e in _stored(wt).settled if e.origin == "operator"]
        assert len(operator) == 1

    def test_the_control_parked_on_both_calls_has_no_warning(
        self, runner: CliRunner, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parked = _park(wt)
        _stage_oscillation(wt)
        spy = _RowSequence(parked)
        monkeypatch.setattr("cw.cli.review.operator_settle._find_ticket", spy)
        result = _operator_settle(runner, _targets(_RECURRING))
        assert result.exit_code == 0, result.stdout + result.stderr
        assert spy.calls == 2
        assert "warning" not in _out(result)
