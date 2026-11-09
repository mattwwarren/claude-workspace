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
from cw.cli.review.converge import ConvergenceConfig, converge_init, merge_tolerance
from cw.dev_queue import find_ticket_row
from cw.events import read_events
from cw.merge_probe import PROBE_TIMEOUT_SECONDS
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    HOOK_CONTEXT_RELATIVE_PATH,
    OPENCODE_BACKEND,
    QueueItemStatus,
    SessionOrigin,
)
from cw.models.client import ReviewConfig
from cw.models.enums import OrchestratorEventType
from cw.review_convergence import (
    DELTA_CONFIRM_ELIGIBLE_KEY,
    LEDGER_RELATIVE_PATH,
    RESOLUTION_FILES_KEY,
    ConvergenceLedger,
    MergeTolerance,
    RoundFinding,
    load_ledger,
    tolerance_for_client,
)
from cw.review_findings import ShardRecord
from cw.spawn import _write_hook_context
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
    fail_when,
    fresh_ledger,
    ledger_with_rounds,
    live_merge_base,
    make_review_client,
    merge_origin_commit,
    operator_finding,
    patch_run_git,
    publish_develop,
    record,
    rewind_origin_main,
    round_finding,
    round_payload,
    stamp_dispositions,
    stopped_ledger,
    write_ledger,
)
from tests.conftest import (
    _make_finding,
    _make_reviewer_doc,
    _make_ticket_task,
    commit_tracked_file,
    git_in,
    push_commit_to_origin,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from click.testing import Result

    from cw.models import TicketTask
    from cw.review_convergence import RoundKind
    from cw.review_findings import Disposition, ReviewVerdict

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


_OMIT = object()


def _context(
    path: Path,
    *,
    convergence: str = "clean",
    ticket: str | None = TICKET_ID,
    headless: bool = True,
    max_cycles: object = None,
    confirm: object = None,
    schema: object = 15,
    default_branch: object = None,
    ignore: object = None,
    tolerance: object = _OMIT,
    engine: object = _OMIT,
) -> Path:
    """Write a cw context shaped like the one ``_write_hook_context`` writes.

    ``default_branch`` (``world_state_snapshot.origin_main_branch``), ``ignore``
    (top-level ``merge_gate_ignore_paths``) and ``tolerance``
    (``review.default_merge_tolerance``) are written only when supplied (#2815).
    ``engine`` (``review.engine``, #3039) is written only when supplied; pass
    ``None`` for the explicit null a context without an engine carries.
    """
    review_obj: dict[str, object] = {
        "convergence": convergence,
        "max_cycles": max_cycles,
        "confirm_backend": confirm,
    }
    if tolerance is not _OMIT:
        review_obj["default_merge_tolerance"] = tolerance
    if engine is not _OMIT:
        review_obj["engine"] = engine
    body: dict[str, object] = {
        "schema_version": schema,
        "session_id": "abc",
        "ticket_id": ticket,
        "headless": headless,
        "review": review_obj,
    }
    if default_branch is not None:
        body["world_state_snapshot"] = {"origin_main_branch": default_branch}
    if ignore is not None:
        body["merge_gate_ignore_paths"] = ignore
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
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

    def test_convergence_config_is_clean(self) -> None:
        """``ConvergenceConfig.is_clean`` mirrors ``ReviewConfig.is_clean`` (#2817)."""
        clean = ConvergenceConfig(
            convergence="clean",
            max_cycles=8,
            confirm_backend_configured=None,
            source="context",
            ticket_id="2817",
        )
        blocking = ConvergenceConfig(
            convergence="blocking",
            max_cycles=5,
            confirm_backend_configured=None,
            source="context",
            ticket_id=None,
        )
        assert clean.is_clean is True
        assert blocking.is_clean is False

    def test_init_loop_backend_default_is_the_claude_native_literal(
        self,
    ) -> None:
        """``init`` stamps ``claude-native`` when the caller passes no backend."""
        option = next(
            param for param in converge_init.params if param.name == "loop_backend"
        )
        assert option.default == CLAUDE_NATIVE_BACKEND == "claude-native"

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
            "engine": None,
            "engine_error": None,
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

    @pytest.mark.parametrize(
        ("confirm", "resolved"),
        [(None, "claude"), ("codex", "codex"), ("none", "none"), ("claude", "claude")],
        ids=["unset", "codex", "none", "claude"],
    )
    def test_opencode_loop_resolves_an_independent_confirm(
        self,
        runner: CliRunner,
        tmp_path: Path,
        confirm: str | None,
        resolved: str,
    ) -> None:
        """#2806: opencode has no confirm family; unset resolves to claude."""
        ctx = _context(tmp_path / "oc.json", confirm=confirm)
        report = self._config(runner, ctx, "--loop-backend", "opencode")
        assert report["confirm_backend"] == resolved
        assert report["confirm_not_independent"] is False
        assert report["loop_backend"] == "opencode"
        assert report["convergence"] == "clean"

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

    def test_opencode_loop_stamps_opencode_loop_and_claude_confirm(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        """#2806: an opencode loop resolves the independent claude confirm."""
        result = _init(runner, wt, ctx, "--loop-backend", "opencode")
        assert result.exit_code == 0, result.output
        ledger = _stored(wt)
        assert ledger.loop_backend == "opencode"
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
    kind: RoundKind,
    backend: str,
    sha: str,
    verdict: ReviewVerdict,
    *,
    roster: Sequence[str] | None = None,
) -> dict[str, object]:
    """A ``record-round`` verdict-form payload (``roster`` defaults to ``ROSTER``)."""
    return {
        "kind": kind,
        "backend": backend,
        "reviewed_sha": sha,
        "roster": list(ROSTER if roster is None else roster),
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
        fork = live_merge_base(wt)
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
        fork = live_merge_base(wt)
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
        rewind_origin_main(wt, merged.fork_point)

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
        publish_develop(wt)
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
        assert _out(result)["diff_base"] == live_merge_base(wt, "develop")

    def test_the_context_branch_is_used_without_the_option(
        self, runner: CliRunner, wt: Path, ctx: Path, tmp_path: Path
    ) -> None:
        _init(runner, wt, ctx)
        publish_develop(wt)
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
        fork = live_merge_base(wt)
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


def _with_shards(verdict: ReviewVerdict, *reviewed: bool) -> ReviewVerdict:
    """*verdict* stamped with one ``ShardRecord`` per *reviewed* flag (#2864)."""
    return verdict.model_copy(
        update={
            "shards": [
                ShardRecord(
                    index=index,
                    files=[f"dir{index}/f.py"],
                    prompt_chars=100,
                    reviewed=flag,
                )
                for index, flag in enumerate(reviewed, start=1)
            ]
        }
    )


class TestShardedConfirmRound:
    """#2864: record-round stores a sharded verdict's tally; finalize reports it."""

    def _awaiting(self, wt: Path) -> bytes:
        write_ledger(wt, ledger_with_rounds([], confirm="codex", shas=[_head(wt)]))
        return _ledger_path(wt).read_bytes()

    def test_a_sharded_round_persists_its_shard_counts(
        self, runner: CliRunner, wt: Path
    ) -> None:
        self._awaiting(wt)
        head = _head(wt)
        verdict = _with_shards(
            consolidated_verdict(accepted=[("MUST_FIX", "x")], sha=head), True, False
        )
        result = _record_verdict(
            runner, wt, _verdict_round("confirm", "codex", head, verdict)
        )
        assert result.exit_code == 0, result.stderr
        stored = _stored(wt).rounds[-1]
        assert (stored.shard_count, stored.shards_reviewed) == (2, 1)

    def test_a_clean_round_with_an_unreviewed_shard_is_refused(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        before = self._awaiting(wt)
        head = _head(wt)
        out = tmp_path / "out-verdict.json"
        verdict = _with_shards(consolidated_verdict(sha=head), True, False)
        result = _record_verdict(
            runner,
            wt,
            _verdict_round("confirm", "codex", head, verdict),
            "--out-verdict",
            str(out),
        )
        assert result.exit_code == 1
        assert "unreviewed_shard:" in result.stderr
        _assert_nothing_recorded(wt, before, out)

    def test_a_deferred_clean_round_with_an_unreviewed_shard_is_refused(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        # The opencode lane records with --defer-should-fix, which rebuilds
        # ``accepted`` before the shard guard reads the verdict (#2806).
        before = self._awaiting(wt)
        head = _head(wt)
        out = tmp_path / "out-verdict.json"
        verdict = _with_shards(
            consolidated_verdict(accepted=[("SHOULD_FIX", "Style")], sha=head),
            True,
            False,
        )
        result = _record_verdict(
            runner,
            wt,
            _verdict_round(
                "confirm", "codex", head, verdict, roster=_agent_roles(verdict)
            ),
            _DEFER_FLAG,
            "--out-verdict",
            str(out),
        )
        assert result.exit_code == 1
        assert "unreviewed_shard:" in result.stderr
        _assert_nothing_recorded(wt, before, out)

    def test_a_deferred_clean_round_persists_its_shards(
        self, runner: CliRunner, wt: Path
    ) -> None:
        self._awaiting(wt)
        head = _head(wt)
        verdict = _with_shards(
            consolidated_verdict(accepted=[("SHOULD_FIX", "Style")], sha=head),
            True,
            True,
        )
        result = _record_verdict(
            runner,
            wt,
            _verdict_round(
                "confirm", "codex", head, verdict, roster=_agent_roles(verdict)
            ),
            _DEFER_FLAG,
        )
        assert result.exit_code == 0, result.stderr
        report = _out(result)
        assert report["decision"] == "clean"
        stored = _stored(wt).rounds[-1]
        assert (stored.shard_count, stored.shards_reviewed) == (2, 2)
        path = (
            wt / ".cw" / "review-convergence" / f"round-{report['round']}-verdict.json"
        )
        persisted: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        assert len(persisted["shards"]) == 2
        (accepted,) = persisted["accepted"]
        assert accepted["disposition"] == "deferred"

    def test_finalize_reports_the_sharded_confirm_round(
        self, runner: CliRunner, wt: Path
    ) -> None:
        self._awaiting(wt)
        head = _head(wt)
        verdict = _with_shards(consolidated_verdict(sha=head), True, True, True)
        recorded = _record_verdict(
            runner, wt, _verdict_round("confirm", "codex", head, verdict)
        )
        assert _out(recorded)["decision"] == "clean"
        final = _converge(runner, wt, "finalize")
        assert final.exit_code == 0, final.stdout + final.stderr
        assert (
            "confirm_round_sharded: round=2 shards=3 reviewed=3"
            in _out(final)["friction_lines"]
        )


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


_DEFER_FLAG = "--defer-should-fix"


def _agent_roles(verdict: ReviewVerdict) -> list[str]:
    """The roster the opencode prompt's jq derives from ``.verdict.agents_run``."""
    return [record.reviewer_role for record in verdict.agents_run]


class TestDeferShouldFix:
    """#2806: ``record-round --defer-should-fix`` decides a raw confirm verdict."""

    def _awaiting(self, wt: Path) -> bytes:
        write_ledger(wt, ledger_with_rounds([], confirm="codex", shas=[_head(wt)]))
        return _ledger_path(wt).read_bytes()

    def _persisted(self, wt: Path, round_number: int) -> dict[str, Any]:
        path = wt / ".cw" / "review-convergence" / f"round-{round_number}-verdict.json"
        body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return body

    def test_a_should_fix_only_confirm_is_clean_and_persisted_deferred(
        self, runner: CliRunner, wt: Path
    ) -> None:
        self._awaiting(wt)
        head = _head(wt)
        verdict = consolidated_verdict(accepted=[("SHOULD_FIX", "Style")], sha=head)
        assert verdict.accepted[0].disposition == "fixed"
        result = _record_verdict(
            runner,
            wt,
            _verdict_round(
                "confirm", "codex", head, verdict, roster=_agent_roles(verdict)
            ),
            _DEFER_FLAG,
        )
        assert result.exit_code == 0, result.stderr
        report = _out(result)
        assert report["decision"] == "clean"
        (accepted,) = self._persisted(wt, report["round"])["accepted"]
        assert accepted["disposition"] == "deferred"
        detail = accepted["disposition_detail"]
        assert "codex" in detail
        assert "confirm" in detail
        assert _DEFER_FLAG in detail
        assert _stored(wt).status == "converged"

    def test_the_same_payload_without_the_flag_continues(
        self, runner: CliRunner, wt: Path
    ) -> None:
        self._awaiting(wt)
        head = _head(wt)
        verdict = consolidated_verdict(accepted=[("SHOULD_FIX", "Style")], sha=head)
        result = _record_verdict(
            runner,
            wt,
            _verdict_round(
                "confirm", "codex", head, verdict, roster=_agent_roles(verdict)
            ),
        )
        assert result.exit_code == 0, result.stderr
        assert _out(result)["decision"] == "continue"

    @pytest.mark.parametrize("disposition", ["dropped", "unresolved"])
    def test_an_undecided_must_fix_with_the_flag_continues(
        self, runner: CliRunner, wt: Path, disposition: Disposition
    ) -> None:
        self._awaiting(wt)
        head = _head(wt)
        verdict = stamp_dispositions(
            consolidated_verdict(accepted=[("MUST_FIX", "Bug")], sha=head),
            {"Bug": (disposition, "")},
        )
        result = _record_verdict(
            runner,
            wt,
            _verdict_round(
                "confirm", "codex", head, verdict, roster=_agent_roles(verdict)
            ),
            _DEFER_FLAG,
        )
        assert result.exit_code == 0, result.stderr
        report = _out(result)
        assert report["decision"] == "continue"
        (accepted,) = self._persisted(wt, report["round"])["accepted"]
        assert accepted["disposition"] == "fixed"

    def test_a_findings_payload_with_the_flag_is_a_usage_error(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        before = self._awaiting(wt)
        result = _round(runner, wt, "confirm", "codex", _head(wt), extra=(_DEFER_FLAG,))
        assert result.exit_code == 2
        assert _DEFER_FLAG in result.stderr
        assert "verdict payload" in result.stderr
        _assert_nothing_recorded(wt, before, tmp_path / "never-written.json")

    def test_help_documents_the_flag(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["review", "converge", "record-round", "--help"])
        assert result.exit_code == 0
        assert _DEFER_FLAG in result.output


_OPENCODE = OPENCODE_BACKEND


class TestOpencodeLaneEndToEnd:
    """#2806: the opencode prompt's command sequence, end to end on real git."""

    def _loop(
        self, runner: CliRunner, wt: Path, *findings: RoundFinding
    ) -> dict[str, Any]:
        payload = round_payload(
            "loop", _OPENCODE, _head(wt), *findings, roster=[_OPENCODE]
        )
        result = _converge(runner, wt, "record-round", "-", payload=payload)
        assert result.exit_code == 0, result.stderr
        return _out(result)

    def _confirm(self, runner: CliRunner, wt: Path) -> dict[str, Any]:
        head = _head(wt)
        verdict = consolidated_verdict(accepted=[("SHOULD_FIX", "Style")], sha=head)
        result = _record_verdict(
            runner,
            wt,
            _verdict_round(
                "confirm", "claude", head, verdict, roster=_agent_roles(verdict)
            ),
            _DEFER_FLAG,
        )
        assert result.exit_code == 0, result.stderr
        return _out(result)

    def _status(self, runner: CliRunner, wt: Path) -> dict[str, Any]:
        result = _converge(runner, wt, "status")
        assert result.exit_code == 0, result.stderr
        return _out(result)

    def _finalize(self, runner: CliRunner, wt: Path) -> dict[str, Any]:
        verify = _converge(runner, wt, "verify-tip")
        assert verify.exit_code == 0, verify.stdout + verify.stderr
        assert _out(verify)["decision"] == "accepted"
        final = _converge(runner, wt, "finalize")
        assert final.exit_code == 0, final.stdout + final.stderr
        return _out(final)

    def _init(self, runner: CliRunner, wt: Path, ctx: Path) -> None:
        result = _init(runner, wt, ctx, "--loop-backend", _OPENCODE)
        assert result.exit_code == 0, result.output

    def test_a_clean_loop_then_a_claude_confirm_converges(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        self._init(runner, wt, ctx)
        assert self._loop(runner, wt)["decision"] == "confirm_required"
        status = self._status(runner, wt)
        assert status["next_action"] == "confirm"
        assert status["sentinel_review"]["confirm_backend"] == "claude"
        assert self._confirm(runner, wt)["decision"] == "clean"
        fragment = self._finalize(runner, wt)["sentinel_review"]
        assert fragment["converged"] is True
        assert fragment["rounds_run"] == 2
        assert fragment["confirm_backend"] == "claude"
        assert fragment["final_round_clean_sha"] == _head(wt)

    def test_a_dirty_round_is_fixed_then_converges(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        self._init(runner, wt, ctx)
        dirty = self._loop(runner, wt, round_finding("MUST_FIX", "Off by one"))
        assert dirty["decision"] == "continue"
        commit_tracked_file(wt, "fix.py")
        git_in(wt, "push", "origin", "feature")
        assert self._loop(runner, wt)["decision"] == "confirm_required"
        assert self._confirm(runner, wt)["decision"] == "clean"
        fragment = self._finalize(runner, wt)["sentinel_review"]
        assert fragment["converged"] is True
        assert fragment["rounds_run"] == 3
        assert fragment["fix_cycles_used"] == 1
        assert fragment["final_round_clean_sha"] == _head(wt)

    def test_a_confirm_outage_parks_retry_eligible(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        self._init(runner, wt, ctx)
        self._loop(runner, wt)
        result = _converge(
            runner,
            wt,
            "confirm-failed",
            "--cause",
            "binary_missing",
            "--detail",
            "claude not on PATH",
            "-c",
            _CLIENT,
        )
        assert result.exit_code == 0, result.stderr
        out = _out(result)
        assert out["decision"] == "stop_confirm_unavailable"
        assert out["blocker"]["reason"] == "confirm_backend_unavailable"
        assert out["blocker"]["retry_eligible"] is True
        assert out["review"]["converged"] is False

    def test_confirm_none_converges_on_the_loop_round_alone(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        self._init(runner, wt, _context(tmp_path / "none.json", confirm="none"))
        assert _stored(wt).confirm_backend == "none"
        assert self._loop(runner, wt)["decision"] == "clean"
        status = self._status(runner, wt)
        assert status["next_action"] == "done"
        assert status["sentinel_review"]["confirm_backend"] == "none"
        fragment = self._finalize(runner, wt)["sentinel_review"]
        assert fragment["converged"] is True
        assert fragment["rounds_run"] == 1
        assert fragment["confirm_backend"] == "none"
        assert fragment["final_round_clean_sha"] == _head(wt)

        before = _ledger_path(wt).read_bytes()
        head = _head(wt)
        verdict = consolidated_verdict(sha=head)
        refused = _record_verdict(
            runner,
            wt,
            _verdict_round(
                "confirm", "none", head, verdict, roster=_agent_roles(verdict)
            ),
            _DEFER_FLAG,
        )
        # A none ledger never reaches awaiting_confirm: it is already converged,
        # so a confirm round (Step 4's defensive branch) has nothing to record.
        assert refused.exit_code == 1
        assert "converged ledger" in refused.stderr
        assert _ledger_path(wt).read_bytes() == before


class TestConfirmFailed:
    """R8 and R26: the confirm backend being down never reads as converged."""

    def test_help_lists_the_confirm_causes(self, runner: CliRunner) -> None:
        """The ``--cause`` help is built from the vocabulary, byte-identical (#2817)."""
        result = runner.invoke(main, ["review", "converge", "confirm-failed", "--help"])

        assert result.exit_code == 0
        flat = " ".join(result.output.split())
        expected = (
            "Why the confirm backend failed "
            "(binary_missing, backend_error, crash, ...)."
        )
        assert expected in flat

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
    patch_run_git(monkeypatch, spy)
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
        assert "default_merge_tolerated" not in out
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
        """R22: any commit after the last clean round, a merge included.

        That holds without a usable default-branch context: no cw-context is
        written here, so the #2815 tolerance cannot apply.
        """
        wt, _ = wt_origin
        before = _pushed_and_converged(wt)
        merge_origin_commit(wt, tmp_path / "side", "main", "main-moved.py")
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
        assert out["reason"] == "tip_check_fetch_failed"
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

        # The fetch is the first git call and raises, so nothing reaches the
        # shared module's rev-parse; patching converge_tip alone is enough.
        monkeypatch.setattr(
            "cw.cli.review.converge_tip.run_git", _raiser(OSError("git vanished"))
        )
        result = _converge(runner, wt, sub)
        assert result.exit_code == 1
        out = _out(result)
        assert out["reason"] == "tip_check_fetch_failed"
        assert "git vanished" in out["detail"]
        assert _ledger_path(wt).read_bytes() == before

    @staticmethod
    def _fetch_answers(
        monkeypatch: pytest.MonkeyPatch, answers: list[tuple[int, str]]
    ) -> list[list[str]]:
        """Stub the tip step's fetch: one ``(rc, stderr)`` per call, in order.

        Every other git call runs for real. Returns the fetch argvs seen.
        """
        fetches: list[list[str]] = []

        def _fake(argv: list[str], **kwargs: Any) -> Any:
            if argv[:1] == ["fetch"]:
                fetches.append(argv)
                rc, stderr = answers[min(len(fetches), len(answers)) - 1]
                return subprocess.CompletedProcess(argv, rc, "", stderr)
            return run_git(argv, **kwargs)

        patch_run_git(monkeypatch, _fake)
        return fetches

    @pytest.mark.parametrize("sub", ["verify-tip", "finalize"])
    def test_a_refused_fetch_is_tip_check_fetch_failed(
        self,
        runner: CliRunner,
        wt: Path,
        monkeypatch: pytest.MonkeyPatch,
        sub: str,
    ) -> None:
        """Reason kill-check (#2910): mapping a refused fetch back to
        ``head_not_origin_tip`` fails this test."""
        before = _pushed_and_converged(wt)
        self._fetch_answers(
            monkeypatch, [(128, "git@github.com: Permission denied (publickey).\n")]
        )

        result = _converge(runner, wt, sub)

        assert result.exit_code == 1
        out = _out(result)
        assert out["decision"] == "rereview_required"
        assert out["reason"] == "tip_check_fetch_failed"
        assert "Permission denied (publickey)" in out["detail"]
        assert out["detail"].startswith("git fetch origin feature failed: ")
        assert _ledger_path(wt).read_bytes() == before

    def test_a_missing_remote_ref_stays_head_not_origin_tip(
        self, runner: CliRunner, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Locale-independent twin of the real never-pushed test below."""
        _pushed_and_converged(wt)
        self._fetch_answers(monkeypatch, [(128, "fatal: couldn't find remote ref x\n")])

        result = _converge(runner, wt, "verify-tip")

        assert result.exit_code == 1
        assert _out(result)["reason"] == "head_not_origin_tip"

    def test_finalize_on_a_fetch_failure_never_saves_the_ledger(
        self, runner: CliRunner, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = _pushed_and_converged(wt)
        self._fetch_answers(monkeypatch, [(128, "fatal: unable to access\n")])
        saves: list[object] = []
        monkeypatch.setattr(
            "cw.cli.review.converge_tip.save_ledger",
            lambda *args, **_kw: saves.append(args),
        )

        result = _converge(runner, wt, "finalize")

        assert result.exit_code == 1
        assert _out(result)["reason"] == "tip_check_fetch_failed"
        assert saves == []
        assert _ledger_path(wt).read_bytes() == before

    def test_finalize_still_demotes_on_commits_after_the_clean_round(
        self, runner: CliRunner, wt: Path
    ) -> None:
        """Regression guard: the new reason did not move the demote branch."""
        _pushed_and_converged(wt)
        commit_tracked_file(wt, "late.py")
        git_in(wt, "push", "origin", "feature")

        result = _converge(runner, wt, "finalize")

        assert result.exit_code == 1
        assert _out(result)["reason"] == "commits_after_clean_round"
        assert _stored(wt).status == "in_progress"

    def test_a_refused_then_approved_fetch_is_accepted_with_no_ledger_change(
        self, runner: CliRunner, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = _pushed_and_converged(wt)
        fetches = self._fetch_answers(
            monkeypatch, [(128, "Permission denied (publickey)\n"), (0, "")]
        )

        first = _converge(runner, wt, "verify-tip")
        second = _converge(runner, wt, "verify-tip")

        assert first.exit_code == 1
        assert _out(first)["reason"] == "tip_check_fetch_failed"
        assert second.exit_code == 0, second.stdout + second.stderr
        assert _out(second) == {"decision": "accepted", "reason": None}
        assert fetches == [["fetch", "origin", "feature"]] * 2
        assert _ledger_path(wt).read_bytes() == before

    def test_an_origin_ref_that_does_not_resolve_fails_closed(
        self, runner: CliRunner, wt: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A branch that was never pushed: the fetch itself fails by name."""
        # The classifier keys on git's English "couldn't find remote ref"; a
        # localized git would read tip_check_fetch_failed here.
        monkeypatch.setenv("LC_ALL", "C")
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

        patch_run_git(monkeypatch, _flaky)
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


_PARKED = {"decision": "rereview_required", "reason": "commits_after_clean_round"}
_SIDE = "side"


def _tip_context(wt: Path, *, tolerance: object = _OMIT, ignore: object = None) -> None:
    """The worktree's own cw context naming ``main`` as the default branch."""
    _context(
        wt / HOOK_CONTEXT_RELATIVE_PATH,
        default_branch="main",
        tolerance=tolerance,
        ignore=ignore,
    )


def _raw_context(wt: Path, text: str) -> None:
    path = wt / HOOK_CONTEXT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _push_main(
    origin: Path, tmp_path: Path, filename: str, content: str = "out-of-band\n"
) -> str:
    """``push_commit_to_origin`` to ``main``, creating *filename*'s parent dirs."""
    side = tmp_path / _SIDE
    if not side.exists():
        git_in(tmp_path, "clone", str(origin), str(side))
    (side / filename).parent.mkdir(parents=True, exist_ok=True)
    return push_commit_to_origin(origin, "main", side, filename, content)


def _sync_merge(
    wt: Path,
    origin: Path,
    tmp_path: Path,
    filename: str,
    content: str = "out-of-band\n",
) -> None:
    """Finalize Step 4c's sync: main moves, ``feature`` merges it and pushes."""
    _push_main(origin, tmp_path, filename, content)
    git_in(wt, "fetch", "origin")
    git_in(wt, "merge", "--no-edit", "origin/main")
    git_in(wt, "push", "origin", "feature")


def _lines(first: str = "line 1", last: str = "line 10") -> str:
    middle = [f"line {n}" for n in range(2, 10)]
    return "\n".join([first, *middle, last]) + "\n"


def _branch_owns(wt: Path, origin: Path, tmp_path: Path, name: str) -> bytes:
    """*name* lands on main, ``feature`` edits its line 1 and converges there."""
    _sync_merge(wt, origin, tmp_path, name, _lines())
    commit_tracked_file(wt, name, _lines(first="feature 1"))
    git_in(wt, "push", "origin", "feature")
    return _pushed_and_converged(wt)


def _touching_merge(wt: Path, origin: Path, tmp_path: Path, name: str) -> bytes:
    """A clean auto-merge of main's line-10 edit to a file the branch changed."""
    before = _branch_owns(wt, origin, tmp_path, name)
    _sync_merge(wt, origin, tmp_path, name, _lines(last="main 10"))
    return before


def _conflicting_merge(wt: Path, origin: Path, tmp_path: Path, name: str) -> bytes:
    """Main edits line 1 of *name* too; the merge stops on a conflict."""
    before = _branch_owns(wt, origin, tmp_path, name)
    _push_main(origin, tmp_path, name, _lines(first="main 1"))
    git_in(wt, "fetch", "origin")
    with pytest.raises(subprocess.CalledProcessError):
        git_in(wt, "merge", "--no-edit", "origin/main")
    return before


def _conclude_merge(wt: Path, *, push: str = "feature") -> None:
    git_in(wt, "commit", "--no-edit")
    git_in(wt, "push", "origin", push)


def _synced(wt: Path, origin: Path, tmp_path: Path) -> tuple[str, bytes]:
    """A converged tip followed by a merge of main that touches no branch file."""
    clean = _head(wt)
    before = _pushed_and_converged(wt)
    _sync_merge(wt, origin, tmp_path, "main-moved.py")
    return clean, before


def _push_other_ref(origin: Path, tmp_path: Path) -> None:
    """``origin/other``: a merge of main's own two commits, itself off main.

    Its changes all lie inside main's change set and it carries no non-merge
    commit that main lacks, so only the "every extra merge parent is an
    ancestor of the default branch" check can refuse a merge of it.
    """
    first = _push_main(origin, tmp_path, "main-a.py")
    _push_main(origin, tmp_path, "main-b.py")
    side = tmp_path / _SIDE
    git_in(side, "checkout", "-B", "other", first)
    git_in(side, "merge", "--no-ff", "--no-edit", "origin/main")
    git_in(side, "push", "origin", "other")


def _push_empty_main_commit(tmp_path: Path) -> None:
    """An empty commit on ``origin/main``: moves main without changing its tree."""
    side = tmp_path / _SIDE
    git_in(side, "checkout", "-B", "main", "origin/main")
    git_in(side, "commit", "--allow-empty", "-m", "empty main commit")
    git_in(side, "push", "origin", "main")


def _push_pr_merge(origin: Path, tmp_path: Path) -> None:
    """A ``--no-ff`` PR merge on ``origin/main``, off a main that moved since.

    The PR branch forks first, main then gains ``main-other.py``, and the PR is
    merged ``--no-ff``: the merge commit differs from its PR parent in
    ``main-other.py``, a file the branch never touched. A listing of every merge
    in the span, rather than the branch's first-parent chain, refuses it.
    """
    side = tmp_path / _SIDE
    _push_main(origin, tmp_path, "main-base.py")
    git_in(side, "checkout", "-B", "pr", "origin/main")
    (side / "pr-change.py").write_text("pr = True\n", encoding="utf-8")
    git_in(side, "add", "pr-change.py")
    git_in(side, "commit", "-m", "pr commit")
    _push_main(origin, tmp_path, "main-other.py")
    git_in(side, "checkout", "-B", "main", "origin/main")
    git_in(side, "merge", "--no-ff", "--no-edit", "pr")
    git_in(side, "push", "origin", "main")


#: The ``detail`` prefix of a rejected move past the last clean round (#2979).
_REJECTION_PREFIX = "verify-tip: not a clean merge of origin/"


def _assert_parks(
    runner: CliRunner,
    wt: Path,
    sub: str,
    before: bytes,
    rejection: str | None = None,
) -> None:
    """The strict verdict: ``verify-tip`` writes nothing, ``finalize`` demotes.

    A rejected move carries a ``detail`` naming why (#2979); *rejection*, when
    given, is the reason's leading token (``not_textually_clean``, ...).
    """
    result = _converge(runner, wt, sub)
    assert result.exit_code == 1, result.stdout + result.stderr
    assert isinstance(result.exception, SystemExit)
    out = _out(result)
    detail = out.pop("detail", None)
    if sub == "verify-tip":
        out.pop(DELTA_CONFIRM_ELIGIBLE_KEY, None)
        out.pop(RESOLUTION_FILES_KEY, None)
    assert out == _PARKED
    if detail is not None:
        assert str(detail).startswith(_REJECTION_PREFIX), detail
    if rejection is not None:
        assert detail is not None
        assert str(detail).split(": ")[-1].startswith(rejection), detail
    if sub == "finalize":
        assert DELTA_CONFIRM_ELIGIBLE_KEY not in out
        assert RESOLUTION_FILES_KEY not in out
        assert _stored(wt).status == "in_progress"
    else:
        assert _ledger_path(wt).read_bytes() == before


def _assert_tolerated(
    runner: CliRunner, wt: Path, sub: str, before: bytes, clean: str | None = None
) -> None:
    """Accepted with the audit key; the ledger is untouched (no demotion)."""
    result = _converge(runner, wt, sub)
    assert result.exit_code == 0, result.stdout + result.stderr
    out = _out(result)
    assert out["decision"] == "accepted"
    assert out["reason"] is None
    assert out["default_merge_tolerated"] is True
    assert _ledger_path(wt).read_bytes() == before
    if sub == "finalize":
        reviewed = clean if clean is not None else _stored(wt).last_clean_sha
        assert out["final_round_clean_sha"] == reviewed
        assert out["final_round_clean_sha"] != _head(wt)
        assert out["sentinel_review"]["final_round_clean_sha"] == reviewed


_SUBS = pytest.mark.parametrize("sub", ["verify-tip", "finalize"])


#: How HEAD moved past the last clean round, for the init/verify-tip agreement.
_MOVES = [
    "tolerated",
    "non-merge-commit",
    "branch-touching-merge",
    "conflict-resolution-merge",
    "strict-context",
    "unreadable-context",
]


class TestDefaultMergeTolerance:
    """#2979: a merge of ``origin/<default>`` git itself would write ships.

    Whichever files it touches (#2815 tolerated only a non-touching merge); a
    conflict resolution or a hand edit in the merge parks with a ``detail``.
    """

    @_SUBS
    def test_a_non_touching_merge_is_accepted(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        clean, before = _synced(wt, origin, tmp_path)
        _assert_tolerated(runner, wt, sub, before, clean)

    @_SUBS
    def test_a_context_without_the_tolerance_key_is_tolerant(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """Only a cw predating the key writes this; no opt-out existed then."""
        wt, origin = wt_origin
        _tip_context(wt)
        review = json.loads((wt / HOOK_CONTEXT_RELATIVE_PATH).read_text())["review"]
        assert "default_merge_tolerance" not in review
        clean, before = _synced(wt, origin, tmp_path)
        _assert_tolerated(runner, wt, sub, before, clean)

    @_SUBS
    def test_an_explicit_true_is_tolerant(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt, tolerance=True)
        clean, before = _synced(wt, origin, tmp_path)
        _assert_tolerated(runner, wt, sub, before, clean)

    @_SUBS
    def test_a_changelog_only_touch_is_accepted_without_an_ignore_list(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        before = _touching_merge(wt, origin, tmp_path, "CHANGELOG.md")
        _assert_tolerated(runner, wt, sub, before)

    @_SUBS
    def test_a_touch_of_an_ignored_path_is_accepted(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt, ignore=["uv.lock"])
        before = _touching_merge(wt, origin, tmp_path, "uv.lock")
        _assert_tolerated(runner, wt, sub, before)

    @_SUBS
    def test_a_default_branch_pr_merge_synced_in_is_accepted(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """The default branch's own ``--no-ff`` PR merge is not the branch's merge.

        It lands in the span as a merge commit reachable only through the
        sync merge's second parent. Only the branch's first-parent merges
        are checked, so that history does not park the tip.
        """
        wt, origin = wt_origin
        _tip_context(wt)
        clean = _head(wt)
        before = _pushed_and_converged(wt)
        _push_pr_merge(origin, tmp_path)
        git_in(wt, "fetch", "origin")
        git_in(wt, "merge", "--no-edit", "origin/main")
        git_in(wt, "push", "origin", "feature")
        assert len(git_in(wt, "rev-list", "--merges", f"{clean}..HEAD").split()) == 2
        _assert_tolerated(runner, wt, sub, before, clean)

    @_SUBS
    def test_a_clean_merge_touching_a_branch_file_is_tolerated(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """#2979: a textually clean merge never stales the tip, overlap or not."""
        wt, origin = wt_origin
        _tip_context(wt)
        before = _touching_merge(wt, origin, tmp_path, "shared.py")
        _assert_tolerated(runner, wt, sub, before)

    @_SUBS
    def test_a_hand_resolved_conflict_in_a_branch_file_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        before = _conflicting_merge(wt, origin, tmp_path, "shared.py")
        (wt / "shared.py").write_text(_lines(first="resolved 1"), encoding="utf-8")
        git_in(wt, "add", "shared.py")
        _conclude_merge(wt)
        merge = _head(wt)
        _assert_parks(runner, wt, sub, before, f"not_textually_clean {merge[:7]}")

    @pytest.mark.parametrize("tolerance", [_OMIT, False], ids=["on", "off"])
    @_SUBS
    def test_a_conflict_resolved_to_the_branch_side_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
        tolerance: object,
    ) -> None:
        """Keeping the branch side verbatim is still a conflict resolution (#2979).

        The file-overlap rule accepted it (the file left ``C``); ``git
        merge-tree`` reports the conflict, so the tip is stale and reviewed.
        """
        wt, origin = wt_origin
        _tip_context(wt, tolerance=tolerance)
        before = _conflicting_merge(wt, origin, tmp_path, "shared.py")
        git_in(wt, "checkout", "--ours", "shared.py")
        git_in(wt, "add", "shared.py")
        _conclude_merge(wt)
        _assert_parks(runner, wt, sub, before)

    @pytest.mark.parametrize("tolerance", [_OMIT, False], ids=["on", "off"])
    @_SUBS
    def test_a_hand_resolved_changelog_conflict_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
        tolerance: object,
    ) -> None:
        """#2979 dropped the ``CHANGELOG.md`` exemption: its conflicts are reviewed."""
        wt, origin = wt_origin
        _tip_context(wt, tolerance=tolerance)
        before = _conflicting_merge(wt, origin, tmp_path, "CHANGELOG.md")
        (wt / "CHANGELOG.md").write_text("anything at all\n", encoding="utf-8")
        git_in(wt, "add", "CHANGELOG.md")
        _conclude_merge(wt)
        _assert_parks(runner, wt, sub, before)

    @pytest.mark.parametrize("touch", ["none", "changelog"])
    @_SUBS
    def test_the_opt_out_restores_the_strict_rule(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
        touch: str,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt, tolerance=False)
        if touch == "none":
            _, before = _synced(wt, origin, tmp_path)
        else:
            before = _touching_merge(wt, origin, tmp_path, "CHANGELOG.md")
        _assert_parks(runner, wt, sub, before)

    @pytest.mark.parametrize(
        "write",
        [
            pytest.param({"tolerance": "false"}, id="tolerance-string"),
            pytest.param({"tolerance": 0}, id="tolerance-zero"),
            pytest.param({"tolerance": None}, id="tolerance-null"),
            pytest.param({"schema": 14}, id="schema-14"),
            pytest.param({"default_branch": None}, id="no-default-branch"),
            pytest.param({"default_branch": 5}, id="non-string-branch"),
            pytest.param({"default_branch": ""}, id="empty-branch"),
            pytest.param({"default_branch": "gone"}, id="unresolvable-branch"),
        ],
    )
    @_SUBS
    def test_an_unusable_context_is_strict(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
        write: dict[str, object],
    ) -> None:
        wt, origin = wt_origin
        fields: dict[str, object] = {"default_branch": "main", **write}
        _context(
            wt / HOOK_CONTEXT_RELATIVE_PATH,
            schema=fields.get("schema", 15),
            default_branch=fields["default_branch"],
            ignore=fields.get("ignore"),
            tolerance=fields.get("tolerance", _OMIT),
        )
        _, before = _synced(wt, origin, tmp_path)
        _assert_parks(runner, wt, sub, before)

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(None, id="no-context-file"),
            pytest.param("{not json", id="malformed-json"),
            pytest.param("[]", id="not-an-object"),
            pytest.param(
                json.dumps(
                    {
                        "schema_version": 15,
                        "world_state_snapshot": {"origin_main_branch": "main"},
                    }
                ),
                id="review-missing",
            ),
            pytest.param(
                json.dumps(
                    {
                        "schema_version": 15,
                        "review": {"convergence": "clean"},
                        "world_state_snapshot": "main",
                    }
                ),
                id="snapshot-not-an-object",
            ),
        ],
    )
    @_SUBS
    def test_an_unreadable_context_is_strict(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
        text: str | None,
    ) -> None:
        wt, origin = wt_origin
        if text is not None:
            _raw_context(wt, text)
        _, before = _synced(wt, origin, tmp_path)
        _assert_parks(runner, wt, sub, before)

    @_SUBS
    def test_a_branch_only_behind_main_is_accepted_on_an_unmoved_head(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """#2979 rule 1: BEHIND is not a reason to act; the tip still ships."""
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        _push_main(origin, tmp_path, "main-moved.py")
        git_in(wt, "fetch", "origin")
        result = _converge(runner, wt, sub)
        assert result.exit_code == 0, result.stdout + result.stderr
        out = _out(result)
        assert out["decision"] == "accepted"
        assert "default_merge_tolerated" not in out
        assert _ledger_path(wt).read_bytes() == before

    def test_a_non_list_ignore_still_tolerates_a_non_touching_merge(
        self, runner: CliRunner, wt_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt, ignore="uv.lock")
        clean, before = _synced(wt, origin, tmp_path)
        _assert_tolerated(runner, wt, "finalize", before, clean)

    @_SUBS
    def test_a_commit_after_a_tolerated_merge_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """Criterion 2: any non-merge commit after the clean round is stale."""
        wt, origin = wt_origin
        _tip_context(wt)
        _, before = _synced(wt, origin, tmp_path)
        commit_tracked_file(wt, "late.py")
        git_in(wt, "push", "origin", "feature")
        _assert_parks(runner, wt, sub, before)

    @_SUBS
    def test_an_empty_commit_after_a_tolerated_merge_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """Only the own-commit check sees it: no path changes, so ``C <= M`` holds."""
        wt, origin = wt_origin
        _tip_context(wt)
        _, before = _synced(wt, origin, tmp_path)
        git_in(wt, "commit", "--allow-empty", "-m", "empty")
        git_in(wt, "push", "origin", "feature")
        _assert_parks(runner, wt, sub, before)

    @_SUBS
    def test_a_commit_reproducing_mains_change_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """The branch's own commit writes what main later lands; the merge is clean.

        Every landed path is one main changed and none is in the branch's
        diff, so only the own-commit check keeps it from skipping re-review.
        """
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        commit_tracked_file(wt, "main-moved.py", "out-of-band\n")
        _sync_merge(wt, origin, tmp_path, "main-moved.py")
        _assert_parks(runner, wt, sub, before)

    @_SUBS
    def test_a_lone_commit_with_the_context_present_parks(
        self, runner: CliRunner, wt: Path, sub: str
    ) -> None:
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        commit_tracked_file(wt, "late.py")
        git_in(wt, "push", "origin", "feature")
        _assert_parks(runner, wt, sub, before)

    @_SUBS
    def test_a_merge_of_another_ref_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        git_in(wt, "push", "origin", "main:other")
        push_commit_to_origin(origin, "other", tmp_path / _SIDE, "other.py")
        git_in(wt, "fetch", "origin")
        git_in(wt, "merge", "--no-edit", "origin/other")
        git_in(wt, "push", "origin", "feature")
        _assert_parks(runner, wt, sub, before)

    @_SUBS
    def test_a_merge_of_a_ref_off_the_default_branch_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """Its paths lie in main's change set and none is a branch file: only the
        extra-parent ancestry check refuses it."""
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        _push_other_ref(origin, tmp_path)
        git_in(wt, "fetch", "origin")
        git_in(wt, "merge", "--no-edit", "origin/other")
        git_in(wt, "push", "origin", "feature")
        _assert_parks(runner, wt, sub, before)

    @_SUBS
    def test_two_successive_non_touching_syncs_are_accepted(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        clean, before = _synced(wt, origin, tmp_path)
        _sync_merge(wt, origin, tmp_path, "main-moved-again.py")
        _assert_tolerated(runner, wt, sub, before, clean)

    @_SUBS
    def test_a_second_clean_sync_touching_a_branch_file_is_tolerated(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        before = _branch_owns(wt, origin, tmp_path, "shared.py")
        _sync_merge(wt, origin, tmp_path, "main-moved.py")
        _sync_merge(wt, origin, tmp_path, "shared.py", _lines(last="main 10"))
        _assert_tolerated(runner, wt, sub, before)

    @_SUBS
    def test_an_earlier_merge_of_a_foreign_ref_parks_behind_a_later_sync(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """The newest merge is an ordinary sync; the older one must still be checked."""
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        _push_other_ref(origin, tmp_path)
        git_in(wt, "fetch", "origin")
        git_in(wt, "merge", "--no-edit", "origin/other")
        _sync_merge(wt, origin, tmp_path, "main-moved.py")
        _assert_parks(runner, wt, sub, before)

    @_SUBS
    def test_an_octopus_merge_with_a_foreign_ref_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """Main is the second parent and the foreign ref the third: both are checked."""
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        _push_other_ref(origin, tmp_path)
        _push_empty_main_commit(tmp_path)
        git_in(wt, "fetch", "origin")
        git_in(wt, "merge", "--no-edit", "origin/main", "origin/other")
        git_in(wt, "push", "origin", "feature")
        assert len(git_in(wt, "rev-list", "--parents", "-n1", "HEAD").split()) == 4
        _assert_parks(runner, wt, sub, before)

    @_SUBS
    def test_an_evil_merge_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """A hand edit outside main's change set rides in the merge and parks.

        ``evil.py`` is outside main's change set (``C <= M`` fails) and also
        outside the branch's diff and the exempt paths, so the merge-add check
        refuses it too; the test does not isolate either check. See
        ``test_a_hand_edit_to_an_exempt_path_main_did_not_change_parks`` for the
        ``C <= M`` check alone.
        """
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        _push_main(origin, tmp_path, "main-moved.py")
        git_in(wt, "fetch", "origin")
        git_in(wt, "merge", "--no-commit", "--no-ff", "origin/main")
        (wt / "evil.py").write_text("smuggled = True\n", encoding="utf-8")
        git_in(wt, "add", "evil.py")
        _conclude_merge(wt)
        _assert_parks(runner, wt, sub, before)

    @_SUBS
    def test_a_hand_edit_to_an_exempt_path_main_did_not_change_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """Only the ``landed <= moved`` (C <= M) check catches this merge.

        ``CHANGELOG.md`` is exempt, so the merge-add check allows it and the
        B & C overlap check ignores it, but main never changed it: the merge
        carries a hand edit outside what main changed. Dropping the
        ``landed <= moved`` clause makes this tip tolerated.
        """
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        _push_main(origin, tmp_path, "main-moved.py")
        git_in(wt, "fetch", "origin")
        git_in(wt, "merge", "--no-commit", "--no-ff", "origin/main")
        (wt / "CHANGELOG.md").write_text("smuggled entry\n", encoding="utf-8")
        git_in(wt, "add", "CHANGELOG.md")
        _conclude_merge(wt)
        _assert_parks(runner, wt, sub, before)

    @_SUBS
    def test_a_hand_edit_to_a_changelog_main_changed_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """No exempt path survives #2979: a hand edit in the merge is reviewed.

        Main adds ``CHANGELOG.md`` and the merge carries a hand edit to it;
        the old ``| exempt`` term shipped that unverified.
        """
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        _push_main(origin, tmp_path, "CHANGELOG.md")
        git_in(wt, "fetch", "origin")
        git_in(wt, "merge", "--no-commit", "--no-ff", "origin/main")
        with (wt / "CHANGELOG.md").open("a", encoding="utf-8") as handle:
            handle.write("hand-resolved entry\n")
        git_in(wt, "add", "CHANGELOG.md")
        _conclude_merge(wt)
        _assert_parks(runner, wt, sub, before, "not_textually_clean")

    @_SUBS
    def test_a_hand_edit_in_a_main_changed_file_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """The file is in C and in M but not in B: ``C <= M`` alone accepts it."""
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        _push_main(origin, tmp_path, "main-moved.py")
        git_in(wt, "fetch", "origin")
        git_in(wt, "merge", "--no-commit", "--no-ff", "origin/main")
        with (wt / "main-moved.py").open("a", encoding="utf-8") as handle:
            handle.write("smuggled = True\n")
        git_in(wt, "add", "main-moved.py")
        _conclude_merge(wt)
        _assert_parks(runner, wt, sub, before)

    @_SUBS
    def test_the_same_merge_without_the_hand_edit_is_tolerated(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """Positive control for the hand-edit test: only the appended line differs."""
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        _push_main(origin, tmp_path, "main-moved.py")
        git_in(wt, "fetch", "origin")
        git_in(wt, "merge", "--no-commit", "--no-ff", "origin/main")
        _conclude_merge(wt)
        _assert_tolerated(runner, wt, sub, before)

    @_SUBS
    def test_a_rewritten_history_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        git_in(wt, "commit", "--amend", "-m", "reworded")
        _push_main(origin, tmp_path, "main-moved.py")
        git_in(wt, "fetch", "origin")
        git_in(wt, "merge", "--no-edit", "origin/main")
        git_in(wt, "push", "--force", "origin", "feature")
        _assert_parks(runner, wt, sub, before)

    @_SUBS
    def test_a_fast_forward_without_a_merge_commit_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        sub: str,
    ) -> None:
        """Only merge commits are tolerated; a fast-forward to main is not one."""
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        git_in(wt, "push", "origin", "feature:main")
        _push_main(origin, tmp_path, "ahead.py")
        git_in(wt, "fetch", "origin")
        git_in(wt, "merge", "--ff-only", "origin/main")
        git_in(wt, "push", "origin", "feature")
        _assert_parks(runner, wt, sub, before)

    @pytest.mark.parametrize(
        ("prefix", "how"),
        [
            (["rev-parse", "refs/remotes/origin/main^{commit}"], "oserror"),
            (["rev-parse", "refs/remotes/origin/main^{commit}"], "rc128"),
            (["merge-base", "--is-ancestor"], "oserror"),
            (["merge-base", "--is-ancestor"], "rc128"),
            (["rev-list", "--reverse", "--no-merges"], "oserror"),
            (["rev-list", "--reverse", "--no-merges"], "rc128"),
            (["rev-list", "--reverse", "--first-parent"], "oserror"),
            (["rev-list", "--reverse", "--first-parent"], "rc128"),
            (["merge-tree"], "oserror"),
            (["merge-tree"], "rc128"),
            (["rev-parse", "<merge>^{tree}"], "rc128"),
            (["rev-parse", "<merge>^{tree}"], "unicode"),
        ],
    )
    @_SUBS
    def test_a_failing_git_call_is_strict(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        sub: str,
        prefix: list[str],
        how: str,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        _, before = _synced(wt, origin, tmp_path)
        merge = _head(wt)
        failed = fail_when(
            monkeypatch, [p.replace("<merge>", merge) for p in prefix], how
        )
        _assert_parks(runner, wt, sub, before)
        assert failed

    @pytest.mark.parametrize("how", ["oserror", "rc128"])
    @_SUBS
    def test_a_failing_clean_ancestor_check_is_strict(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        sub: str,
        how: str,
    ) -> None:
        """Fail only the first ``merge-base --is-ancestor`` (clean is behind HEAD).

        ``test_a_failing_git_call_is_strict`` fails every ``--is-ancestor``
        call, the per-parent ones included. Here the per-parent calls stay
        real, so only the first call's ``is None`` refusal can park the tip.
        """
        wt, origin = wt_origin
        _tip_context(wt)
        _, before = _synced(wt, origin, tmp_path)
        failed = fail_when(monkeypatch, ["merge-base", "--is-ancestor"], how, nth=0)
        _assert_parks(runner, wt, sub, before)
        assert len(failed) == 1

    def test_init_keeps_the_ledger_converged_across_a_tolerated_merge(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        ctx: Path,
    ) -> None:
        """#2861: init and status share verify-tip's predicate (was #2821)."""
        wt, origin = wt_origin
        _tip_context(wt)
        clean, before = _synced(wt, origin, tmp_path)
        assert _head(wt) != clean
        _assert_tolerated(runner, wt, "verify-tip", before, clean)
        report = _out(_init(runner, wt, ctx))
        assert report["status"] == "converged"
        assert report["reset_reason"] is None
        assert "released" not in report
        assert report["default_merge_tolerated"] is True
        assert _ledger_path(wt).read_bytes() == before
        status = _out(_converge(runner, wt, "status"))
        assert status["next_action"] == "done"
        assert status["default_merge_tolerated"] is True
        _assert_tolerated(runner, wt, "finalize", before, clean)

    def test_init_on_an_unmoved_tip_reports_no_tolerance_key(
        self, runner: CliRunner, wt: Path, ctx: Path
    ) -> None:
        _tip_context(wt)
        _pushed_and_converged(wt)
        report = _out(_init(runner, wt, ctx))
        assert set(report) == {
            "convergence",
            "status",
            "max_cycles",
            "confirm_backend",
            "confirm_not_independent",
            "reset_reason",
        }
        status = _out(_converge(runner, wt, "status"))
        assert status["next_action"] == "done"
        assert "default_merge_tolerated" not in status

    @pytest.mark.parametrize("shape", _MOVES)
    def test_init_and_status_agree_with_verify_tip(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        ctx: Path,
        shape: str,
    ) -> None:
        """Keep-or-demote matches verify-tip's accept-or-commits_after_clean_round."""
        wt, origin = wt_origin
        _move(shape, wt, origin, tmp_path)
        kept = shape in {"tolerated", "branch-touching-merge"}
        _assert_init_agrees(runner, wt, ctx, kept=kept)

    @pytest.mark.parametrize("backend", ["codex", "opencode"])
    def test_every_surface_tolerates_a_non_claude_native_ledger(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        ctx: Path,
        backend: str,
    ) -> None:
        """#2979 removed the claude-native allow-list: the rule is backend-free.

        ``verify-tip``, ``finalize``, ``init`` and ``status`` all keep a codex or
        opencode ledger converged across a clean default-branch merge.
        """
        wt, origin = wt_origin
        _tip_context(wt)
        clean = _head(wt)
        write_ledger(
            wt,
            confirmed_ledger(sha=clean).model_copy(update={"loop_backend": backend}),
        )
        before = _ledger_path(wt).read_bytes()
        _sync_merge(wt, origin, tmp_path, "main-moved.py")
        assert _head(wt) != clean
        _assert_tolerated(runner, wt, "verify-tip", before, clean)
        _assert_tolerated(runner, wt, "finalize", before, clean)
        report = _out(_init(runner, wt, ctx))
        assert report["status"] == "converged"
        assert report["default_merge_tolerated"] is True
        status = _out(_converge(runner, wt, "status"))
        assert status["next_action"] == "done"

    @pytest.mark.parametrize("how", ["oserror", "rc128"])
    def test_unreadable_git_state_demotes_like_verify_tip_parks(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        ctx: Path,
        monkeypatch: pytest.MonkeyPatch,
        how: str,
    ) -> None:
        wt, origin = wt_origin
        _move("tolerated", wt, origin, tmp_path)
        failed = fail_when(monkeypatch, ["rev-list", "--reverse", "--no-merges"], how)
        _assert_init_agrees(runner, wt, ctx, kept=False)
        assert failed


def _move(shape: str, wt: Path, origin: Path, tmp_path: Path) -> None:
    """A ledger converged at an older tip, then HEAD moved in the named way."""
    _tip_context(wt, tolerance=False if shape == "strict-context" else _OMIT)
    if shape == "unreadable-context":
        _raw_context(wt, "{not json")
    if shape == "non-merge-commit":
        _pushed_and_converged(wt)
        commit_tracked_file(wt, "later.py")
        git_in(wt, "push", "origin", "feature")
    elif shape == "branch-touching-merge":
        _touching_merge(wt, origin, tmp_path, "shared.py")
    elif shape == "conflict-resolution-merge":
        _conflicting_merge(wt, origin, tmp_path, "shared.py")
        (wt / "shared.py").write_text("hand resolved\n", encoding="utf-8")
        git_in(wt, "add", "shared.py")
        _conclude_merge(wt)
    else:
        _synced(wt, origin, tmp_path)


def _assert_init_agrees(runner: CliRunner, wt: Path, ctx: Path, *, kept: bool) -> None:
    """verify-tip, then init, then status: all three keep or all three refuse."""
    tip = _out(_converge(runner, wt, "verify-tip"))
    report = _out(_init(runner, wt, ctx))
    status = _out(_converge(runner, wt, "status"))
    assert (tip["decision"] == "accepted") is kept
    assert (report["status"] == "converged") is kept
    assert (status["next_action"] == "done") is kept
    if kept:
        assert report["default_merge_tolerated"] is True
        assert status["default_merge_tolerated"] is True
        return
    assert tip["reason"] == "commits_after_clean_round"
    assert report["status"] == "in_progress"
    assert report["reset_reason"] is None
    assert "default_merge_tolerated" not in report
    assert "default_merge_tolerated" not in status
    assert _stored(wt).status == "in_progress"
    assert status["next_action"] == "loop_round"


_FETCH_DEFAULT = [
    "fetch",
    "origin",
    "+refs/heads/main:refs/remotes/origin/main",
]


def _remote_update_branch(wt: Path, origin: Path, tmp_path: Path) -> None:
    """A clean merge of NEWER main made elsewhere (GitHub's "Update branch").

    ``main`` moves on origin, a second clone merges it into ``feature`` and
    pushes, and ``wt`` only fast-forwards to the pushed ``feature``: its
    ``origin/main`` tracking ref keeps the old main, so the merge's second
    parent is not on the default branch as far as ``wt`` can see.
    """
    _push_main(origin, tmp_path, "main-moved.py")
    side = tmp_path / _SIDE
    git_in(side, "fetch", "origin")
    git_in(side, "checkout", "-B", "feature", "origin/feature")
    git_in(side, "merge", "--no-edit", "origin/main")
    git_in(side, "push", "origin", "feature")
    git_in(wt, "fetch", "origin", "feature")
    git_in(wt, "merge", "--ff-only", "origin/feature")


def _default_fetches(spy: _GitSpy) -> int:
    return sum(1 for argv, _cwd in spy.calls if argv == _FETCH_DEFAULT)


class TestStaleDefaultRefRefresh:
    """A merge of newer main is tolerated after one bounded refresh of the ref."""

    @_SUBS
    def test_a_merge_of_newer_main_is_tolerated_after_the_refresh(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        git_spy: _GitSpy,
        sub: str,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        clean = _head(wt)
        before = _pushed_and_converged(wt)
        _remote_update_branch(wt, origin, tmp_path)
        stale_main = git_in(wt, "rev-parse", "origin/main")

        _assert_tolerated(runner, wt, sub, before, clean)

        assert _default_fetches(git_spy) == 1
        assert git_in(wt, "rev-parse", "origin/main") != stale_main

    @_SUBS
    def test_a_missing_default_ref_is_repaired_by_the_refresh(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        git_spy: _GitSpy,
        sub: str,
    ) -> None:
        """A deleted ``origin/main`` reads ``no_default_ref``.

        The fetch recreates the tracking ref, so the clean merge of main is
        tolerated after exactly one refresh. (A real single-branch clone is
        the next test: it needs the explicit refspec.)
        """
        wt, origin = wt_origin
        _tip_context(wt)
        clean = _head(wt)
        before = _pushed_and_converged(wt)
        _remote_update_branch(wt, origin, tmp_path)
        git_in(wt, "update-ref", "-d", "refs/remotes/origin/main")

        _assert_tolerated(runner, wt, sub, before, clean)

        assert _default_fetches(git_spy) == 1
        assert git_in(wt, "rev-parse", "--verify", "origin/main")

    @_SUBS
    def test_a_single_branch_clone_gets_its_default_ref_from_the_refresh(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        git_spy: _GitSpy,
        sub: str,
    ) -> None:
        """A real ``--single-branch`` clone: a bare ``fetch origin main`` fails here.

        Its configured refspec tracks only ``feature``, so ``git fetch origin
        main`` writes ``FETCH_HEAD`` and never creates ``origin/main``; only the
        explicit refspec does.
        """
        wt, origin = wt_origin
        clean = _head(wt)
        _remote_update_branch(wt, origin, tmp_path)
        single = tmp_path / "single"
        git_in(
            tmp_path,
            "clone",
            "--single-branch",
            "--branch",
            "feature",
            str(origin),
            str(single),
        )
        assert "origin/main" not in git_in(single, "branch", "-r")
        _tip_context(single)
        write_ledger(single, confirmed_ledger(sha=clean))
        before = _ledger_path(single).read_bytes()

        _assert_tolerated(runner, single, sub, before, clean)

        assert _default_fetches(git_spy) == 1
        assert git_in(single, "rev-parse", "--verify", "origin/main")

    @_SUBS
    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(128, id="refused"),
            pytest.param(subprocess.TimeoutExpired(["git"], 10.0), id="timeout"),
            pytest.param(OSError("git vanished"), id="cannot-run"),
        ],
    )
    def test_a_failed_refresh_keeps_the_stale_verdict(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        sub: str,
        failure: int | BaseException,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        _remote_update_branch(wt, origin, tmp_path)
        spy = _GitSpy(run_git)
        timeouts: list[object] = []

        def _fetch_default_fails(argv: list[str], **kwargs: Any) -> Any:
            if argv != _FETCH_DEFAULT:
                return spy(argv, **kwargs)
            spy.calls.append((list(argv), str(kwargs.get("cwd"))))
            timeouts.append(kwargs.get("timeout"))
            if isinstance(failure, BaseException):
                raise failure
            return subprocess.CompletedProcess(argv, failure, "", "fatal: refused")

        patch_run_git(monkeypatch, _fetch_default_fails)

        _assert_parks(runner, wt, sub, before, "non_merge_commit")

        assert _default_fetches(spy) == 1
        assert timeouts == [PROBE_TIMEOUT_SECONDS]

    @_SUBS
    def test_a_merge_of_a_foreign_ref_still_parks_after_the_refresh(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        git_spy: _GitSpy,
        sub: str,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        _push_other_ref(origin, tmp_path)
        git_in(wt, "fetch", "origin")
        git_in(wt, "merge", "--no-edit", "origin/other")
        git_in(wt, "push", "origin", "feature")

        _assert_parks(runner, wt, sub, before, "second_parent_not_on_default")

        assert _default_fetches(git_spy) == 1

    @_SUBS
    def test_a_hand_edited_merge_does_not_fetch_the_default_branch(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        git_spy: _GitSpy,
        sub: str,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        before = _pushed_and_converged(wt)
        _push_main(origin, tmp_path, "main-moved.py")
        git_in(wt, "fetch", "origin")
        git_in(wt, "merge", "--no-commit", "--no-ff", "origin/main")
        (wt / "evil.py").write_text("smuggled = True\n", encoding="utf-8")
        git_in(wt, "add", "evil.py")
        _conclude_merge(wt)

        _assert_parks(runner, wt, sub, before, "not_textually_clean")

        assert _default_fetches(git_spy) == 0

    @_SUBS
    def test_a_branch_commit_after_the_clean_round_parks_after_one_fetch(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        git_spy: _GitSpy,
        sub: str,
    ) -> None:
        """A real own commit reads the same as a stale ref: one fetch, still stale."""
        wt, origin = wt_origin
        _tip_context(wt)
        _, before = _synced(wt, origin, tmp_path)
        commit_tracked_file(wt, "late.py")
        git_in(wt, "push", "origin", "feature")

        _assert_parks(runner, wt, sub, before, "non_merge_commit")

        assert _default_fetches(git_spy) == 1

    @_SUBS
    def test_a_strict_context_never_fetches_the_default_branch(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        git_spy: _GitSpy,
        sub: str,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt, tolerance=False)
        before = _pushed_and_converged(wt)
        _remote_update_branch(wt, origin, tmp_path)

        _assert_parks(runner, wt, sub, before)

        assert _default_fetches(git_spy) == 0


class TestToleranceContextProducer:
    """``merge_tolerance`` against the context ``_write_hook_context`` writes.

    The converge tests hand-build their contexts, so a producer-side rename of
    ``world_state_snapshot.origin_main_branch``, ``merge_gate_ignore_paths`` or
    ``review.default_merge_tolerance`` would make every merge strict without
    turning any of them red. This reads the real writer's output back, and pins
    it against :func:`tolerance_for_client`, the gate's reader of the same
    client fields (#2861).
    """

    @staticmethod
    def _produce(wt: Path, *, review: ReviewConfig) -> None:
        _write_hook_context(
            wt,
            session_id="sess-tip",
            session_name="acme/auto-dev/2815",
            client=_CLIENT,
            purpose="impl",
            ticket_id=TICKET_ID,
            origin=SessionOrigin.DAEMON,
            task=_make_ticket_task(ticket_id=TICKET_ID, client=_CLIENT),
            default_branch="trunk",
            merge_gate_ignore_paths=["docs/generated.md"],
            review_config=review,
            write_stop_hook=False,
        )

    def test_the_real_writer_output_yields_the_branch(self, tmp_path: Path) -> None:
        """The ignore list rides the context for Stage 4a only, never the tip rule."""
        self._produce(tmp_path, review=ReviewConfig(convergence="clean"))
        assert merge_tolerance(tmp_path) == MergeTolerance("trunk")

    def test_the_real_writers_opt_out_is_strict(self, tmp_path: Path) -> None:
        self._produce(
            tmp_path,
            review=ReviewConfig(convergence="clean", default_merge_tolerance=False),
        )
        assert merge_tolerance(tmp_path) is None

    @pytest.mark.parametrize("tolerant", [True, False])
    def test_the_client_reader_matches_the_context_reader(
        self, tmp_path: Path, tolerant: bool
    ) -> None:
        """Parity: the gate's client-side reader agrees with the context side."""
        client = make_review_client(
            tmp_path,
            name=_CLIENT,
            default_branch="trunk",
            ignore_paths=["docs/generated.md"],
            default_merge_tolerance=tolerant,
        )
        self._produce(tmp_path, review=client.review)
        assert tolerance_for_client(client) == merge_tolerance(tmp_path)


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


def _resolved_conflict(
    wt: Path, origin: Path, tmp_path: Path, *, extra: str | None = None
) -> bytes:
    """A converged tip, then a merge of main whose conflict is resolved by hand.

    *extra* names a new file the resolution also adds, so the resolution
    touches two files.
    """
    before = _conflicting_merge(wt, origin, tmp_path, "shared.py")
    (wt / "shared.py").write_text(_lines(first="resolved 1"), encoding="utf-8")
    git_in(wt, "add", "shared.py")
    if extra is not None:
        (wt / extra).write_text("extra = 1\n", encoding="utf-8")
        git_in(wt, "add", extra)
    _conclude_merge(wt)
    return before


def _eligible_keys(out: dict[str, Any]) -> tuple[object, object]:
    return out.get(DELTA_CONFIRM_ELIGIBLE_KEY), out.get(RESOLUTION_FILES_KEY)


class TestDeltaConfirmEligibility:
    """``verify-tip`` names a move ``delta-confirm`` could review (#2980)."""

    def test_a_resolved_conflict_is_eligible(
        self, runner: CliRunner, wt_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        before = _resolved_conflict(wt, origin, tmp_path)
        merge = _head(wt)

        result = _converge(runner, wt, "verify-tip")

        assert result.exit_code == 1, result.stdout + result.stderr
        out = _out(result)
        assert _eligible_keys(out) == (True, ["shared.py"])
        assert out["detail"].startswith(_REJECTION_PREFIX)
        assert out["detail"].endswith(f"not_textually_clean {merge[:7]}")
        assert _ledger_path(wt).read_bytes() == before

    def test_two_resolved_files_are_sorted(
        self, runner: CliRunner, wt_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        _resolved_conflict(wt, origin, tmp_path, extra="a_extra.py")

        out = _out(_converge(runner, wt, "verify-tip"))

        assert _eligible_keys(out) == (True, ["a_extra.py", "shared.py"])

    def test_a_commit_after_the_resolution_is_not_eligible(
        self, runner: CliRunner, wt_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        _resolved_conflict(wt, origin, tmp_path)
        commit_tracked_file(wt, "late.py")
        git_in(wt, "push", "origin", "feature")

        out = _out(_converge(runner, wt, "verify-tip"))

        assert out["detail"].split(": ")[-1].startswith("non_merge_commit")
        assert _eligible_keys(out) == (None, None)

    def test_two_resolutions_are_not_eligible(
        self, runner: CliRunner, wt_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        _resolved_conflict(wt, origin, tmp_path)
        _push_main(origin, tmp_path, "shared.py", _lines(first="main again"))
        git_in(wt, "fetch", "origin")
        with pytest.raises(subprocess.CalledProcessError):
            git_in(wt, "merge", "--no-edit", "origin/main")
        (wt / "shared.py").write_text(_lines(first="resolved 2"), encoding="utf-8")
        git_in(wt, "add", "shared.py")
        _conclude_merge(wt)

        out = _out(_converge(runner, wt, "verify-tip"))

        assert out["detail"].split(": ")[-1].startswith("not_textually_clean")
        assert _eligible_keys(out) == (None, None)

    def test_a_strict_context_names_no_eligibility(
        self, runner: CliRunner, wt_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt, tolerance=False)
        _resolved_conflict(wt, origin, tmp_path)

        out = _out(_converge(runner, wt, "verify-tip"))

        assert _eligible_keys(out) == (None, None)

    def test_a_ledger_without_an_independent_confirm_is_not_eligible(
        self, runner: CliRunner, wt_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        _resolved_conflict(wt, origin, tmp_path)
        clean = _stored(wt).last_clean_sha
        assert clean is not None
        write_ledger(wt, confirmed_ledger(sha=clean, confirm="none"))

        out = _out(_converge(runner, wt, "verify-tip"))

        assert out["detail"].split(": ")[-1].startswith("not_textually_clean")
        assert _eligible_keys(out) == (None, None)

    def test_a_ledger_not_finalize_ready_names_no_eligibility(
        self, runner: CliRunner, wt_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        _resolved_conflict(wt, origin, tmp_path)
        write_ledger(wt, fresh_ledger())

        out = _out(_converge(runner, wt, "verify-tip"))

        assert out["reason"] == "no_clean_round"
        assert _eligible_keys(out) == (None, None)

    def test_finalize_demotes_and_never_assesses(
        self,
        runner: CliRunner,
        wt_origin: tuple[Path, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wt, origin = wt_origin
        _tip_context(wt)
        _resolved_conflict(wt, origin, tmp_path)
        assessed: list[object] = []

        def _spy(*args: object, **kwargs: object) -> object:
            assessed.append((args, kwargs))
            raise AssertionError

        monkeypatch.setattr("cw.cli.review.converge_tip.assess_resolution_round", _spy)

        result = _converge(runner, wt, "finalize")

        assert result.exit_code == 1, result.stdout + result.stderr
        out = _out(result)
        assert _eligible_keys(out) == (None, None)
        assert out["detail"].split(": ")[-1].startswith("not_textually_clean")
        assert _stored(wt).status == "in_progress"
        assert assessed == []


# ---------------------------------------------------------------------------
# RFC 0017 I1 (#3039): engine config keys, operator-settle --outcome defer
# ---------------------------------------------------------------------------

_ENGINE_FP = "fp-000001"
_ENGINE_FILE = "src/cw/foo.py"
_ENGINE_SUMMARY = "Engine finding needing a human"
_DEFER_REASON = "operator accepts this engine MUST_FIX for the run"


class TestConfigEngine:
    """``converge config`` reports ``engine`` / ``engine_error`` (#3039)."""

    def _config(self, runner: CliRunner, ctx: Path, *args: str) -> dict[str, Any]:
        result = runner.invoke(
            main, ["review", "converge", "config", "--context", str(ctx), *args]
        )
        assert result.exit_code == 0, result.output
        return _out(result)

    def _engine_ctx(self, tmp_path: Path, **overrides: Any) -> Path:
        kwargs: dict[str, Any] = {
            "engine": {"skill": "repo-review", "guarded_paths": ["scripts/*.py"]}
        }
        kwargs.update(overrides)
        return _context(tmp_path / "c.json", **kwargs)

    def test_claude_native_clean_reports_the_skill_name_only(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        report = self._config(runner, self._engine_ctx(tmp_path))

        assert report["engine"] == "repo-review"
        assert report["engine_error"] is None
        assert "scripts" not in json.dumps(report)

    @pytest.mark.parametrize("backend", ["codex", "opencode", "some-future-backend"])
    def test_other_loop_backends_report_null(
        self, runner: CliRunner, tmp_path: Path, backend: str
    ) -> None:
        report = self._config(
            runner, self._engine_ctx(tmp_path), "--loop-backend", backend
        )

        assert report["engine"] is None
        assert report["engine_error"] is None
        assert report["convergence"] == "clean"

    def test_blocking_context_reports_null_even_with_an_engine_block(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        ctx = self._engine_ctx(tmp_path, convergence="blocking")

        report = self._config(runner, ctx)

        assert report["engine"] is None
        assert report["engine_error"] is None

    def test_context_below_schema_15_reports_null(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        report = self._config(runner, self._engine_ctx(tmp_path, schema=14))

        assert report["engine"] is None
        assert report["engine_error"] is None

    def test_explicit_null_engine_reports_null(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        report = self._config(runner, _context(tmp_path / "c.json", engine=None))

        assert report["engine"] is None
        assert report["engine_error"] is None

    @pytest.mark.parametrize(
        ("engine", "reason"),
        [
            ("repo-review", "not an object"),
            ({"guarded_paths": []}, "skill is missing"),
            ({"skill": ""}, "skill is missing"),
            ({"skill": 7}, "skill is missing"),
        ],
        ids=["not-an-object", "no-skill", "empty-skill", "non-string-skill"],
    )
    def test_malformed_engine_reports_engine_error(
        self, runner: CliRunner, tmp_path: Path, engine: object, reason: str
    ) -> None:
        report = self._config(runner, self._engine_ctx(tmp_path, engine=engine))

        assert report["engine"] is None
        assert reason in report["engine_error"]

    def test_engine_error_is_hidden_from_a_codex_lane(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        ctx = self._engine_ctx(tmp_path, engine="oops")

        report = self._config(runner, ctx, "--loop-backend", "codex")

        assert report["engine"] is None
        assert report["engine_error"] is None


def _engine_na_finding(
    severity: str = "MUST_FIX",
    *,
    fingerprint: str = _ENGINE_FP,
    outcome: str = "operator_action",
    rule_id: str = "style.naming",
) -> RoundFinding:
    return RoundFinding.model_validate(
        {
            "severity": severity,
            "file": "N/A" if outcome == "operator_action" else _ENGINE_FILE,
            "summary": _ENGINE_SUMMARY,
            "outcome": outcome,
            "rationale": "needs an operator",
            "fingerprint": fingerprint,
            "rule_id": rule_id,
        }
    )


def _stage_engine_operator_action(
    wt: Path,
    *,
    status: QueueItemStatus = QueueItemStatus.BLOCKED_ON_USER,
    ticket_id: str = TICKET_ID,
) -> bytes:
    """A parked row plus a ledger stopped ``operator_action`` on an engine MUST_FIX."""
    _park(wt, status=status)
    ledger, report = record(
        fresh_ledger(ticket_id=ticket_id), _engine_na_finding(), sha=_head(wt)
    )
    assert report.decision == "stop_operator_action"
    write_ledger(wt, ledger)
    return _ledger_path(wt).read_bytes()


def _engine_targets(*, fingerprint: str | None = _ENGINE_FP) -> dict[str, object]:
    entry: dict[str, object] = {
        "severity": "MUST_FIX",
        "file": "N/A",
        "summary": _ENGINE_SUMMARY,
    }
    if fingerprint is not None:
        entry["fingerprint"] = fingerprint
    return {"entries": [entry]}


def _defer(
    runner: CliRunner, payload: object, *extra: str, reason: str = _DEFER_REASON
) -> Result:
    return _operator_settle(
        runner, payload, "--outcome", "defer", *extra, reason=reason
    )


class TestOperatorSettleDefer:
    """``operator-settle --outcome defer`` accepts an engine MUST_FIX (#3039)."""

    @freeze_time("2026-10-27 12:00:00")
    def test_defer_writes_an_operator_origin_entry_and_the_audit_event(
        self, runner: CliRunner, wt: Path
    ) -> None:
        head = _head(wt)
        _stage_engine_operator_action(wt)

        result = _defer(runner, _engine_targets())

        assert result.exit_code == 0, result.stdout + result.stderr
        out = _out(result)
        assert out["unmatched"] == []
        assert out["releasable"] is True
        entry = _stored(wt).settled[-1]
        assert (entry.origin, entry.outcome) == ("operator", "defer")
        assert entry.actor == _OPERATOR
        assert entry.rationale == _DEFER_REASON
        assert entry.settled_at == "2026-10-27T12:00:00Z"
        assert entry.reviewed_sha == head
        assert entry.identity.fingerprint == _ENGINE_FP
        assert entry.identity.severity == "MUST_FIX"
        events = _settle_events()
        assert len(events) == 1
        assert events[0].correlation_id == TICKET_ID
        assert events[0].payload == {
            "key": f"engine::{_ENGINE_FP}",
            "file": "N/A",
            "summary": _ENGINE_SUMMARY,
            "outcome": "DEFERRED",
            "reason": _DEFER_REASON,
            "actor": _OPERATOR,
            "recorded_at": "2026-10-27T12:00:00Z",
            "reviewed_sha": head,
        }

    def test_defer_without_a_fingerprint_is_refused_and_writes_nothing(
        self, runner: CliRunner, wt: Path
    ) -> None:
        before = _stage_engine_operator_action(wt)

        result = _defer(runner, _engine_targets(fingerprint=None))

        assert result.exit_code == 1
        assert "needs an engine fingerprint" in result.stderr
        assert _ledger_path(wt).read_bytes() == before
        assert _settle_events() == []

    def test_reject_of_an_engine_fingerprint_is_refused(
        self, runner: CliRunner, wt: Path
    ) -> None:
        before = _stage_engine_operator_action(wt)

        result = _operator_settle(runner, _engine_targets())

        assert result.exit_code == 1
        assert "cannot be rejected" in result.stderr
        assert _ledger_path(wt).read_bytes() == before
        assert _settle_events() == []

    def test_defer_inside_a_headless_worker_is_refused(
        self, runner: CliRunner, wt: Path, tmp_path: Path
    ) -> None:
        before = _stage_engine_operator_action(wt)
        _write_session_context(tmp_path, headless=True)

        result = _defer(runner, _engine_targets())

        assert result.exit_code == 1
        assert "Refusing to settle" in result.stderr
        assert _ledger_path(wt).read_bytes() == before
        assert _settle_events() == []

    def test_defer_while_the_row_is_running_is_refused(
        self, runner: CliRunner, wt: Path
    ) -> None:
        before = _stage_engine_operator_action(wt, status=QueueItemStatus.RUNNING)

        result = _defer(runner, _engine_targets())

        assert result.exit_code == 1
        assert "RUNNING" in result.stderr
        assert _ledger_path(wt).read_bytes() == before
        assert _settle_events() == []

    def test_a_foreign_ticket_ledger_is_still_refused(
        self, runner: CliRunner, wt: Path
    ) -> None:
        before = _stage_engine_operator_action(wt, ticket_id="T-OTHER")

        result = _defer(runner, _engine_targets())

        assert result.exit_code == 1
        assert "belongs to ticket T-OTHER" in result.stderr
        assert _ledger_path(wt).read_bytes() == before
        assert _settle_events() == []

    def test_an_unmatched_fingerprint_prints_the_nearest_identity(
        self, runner: CliRunner, wt: Path
    ) -> None:
        before = _stage_engine_operator_action(wt)

        result = _defer(runner, _engine_targets(fingerprint="fp-000009"))

        assert result.exit_code == 1
        out = _out(result)
        assert out["written"] is False
        assert len(out["unmatched"]) == 1
        assert out["unmatched"][0]["nearest"]["fingerprint"] == _ENGINE_FP
        assert _ledger_path(wt).read_bytes() == before
        assert _settle_events() == []

    def test_defer_is_idempotent_one_entry_per_fingerprint_newest_wins(
        self, runner: CliRunner, wt: Path
    ) -> None:
        _stage_engine_operator_action(wt)

        first = _defer(runner, _engine_targets(), reason="first reason")
        second = _defer(runner, _engine_targets(), reason="second reason")

        assert first.exit_code == 0, first.stderr
        assert second.exit_code == 0, second.stderr
        settled = [
            e for e in _stored(wt).settled if e.identity.fingerprint == _ENGINE_FP
        ]
        assert [e.rationale for e in settled] == ["second reason"]
        assert len(_settle_events()) == 2

    def test_explicit_reject_outcome_matches_the_default(
        self, runner: CliRunner, wt: Path
    ) -> None:
        _stage_oscillation(wt)

        result = _operator_settle(runner, _targets(_RECURRING), "--outcome", "reject")

        assert result.exit_code == 0, result.stdout + result.stderr
        entry = _stored(wt).settled[-1]
        assert (entry.origin, entry.outcome) == ("operator", "reject")
        assert entry.identity.fingerprint is None
        events = _settle_events()
        assert events[0].payload["outcome"] == "REJECTED"
        assert events[0].payload["key"] == f"MUST_FIX::{_ENGINE_FILE}::{_RECURRING}"

    def test_invalid_outcome_is_a_usage_error(
        self, runner: CliRunner, wt: Path
    ) -> None:
        _stage_oscillation(wt)

        result = _operator_settle(runner, _targets(_RECURRING), "--outcome", "fix")

        assert result.exit_code == 2

    def test_status_settle_payload_carries_fingerprint(
        self, runner: CliRunner, wt: Path
    ) -> None:
        _stage_engine_operator_action(wt)

        status = _out(_converge(runner, wt, "status"))

        stop = status["stop"]
        assert stop["reason"] == "operator_action"
        assert stop["identities"][0]["fingerprint"] == _ENGINE_FP
        entry = stop["settle_payload"]["entries"][0]
        assert entry["fingerprint"] == _ENGINE_FP

        # The payload pasted verbatim matches: no `unmatched`.
        result = _defer(runner, stop["settle_payload"])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert _out(result)["unmatched"] == []

    def test_status_settle_payload_for_builtin_identity_is_byte_identical(
        self, runner: CliRunner, wt: Path
    ) -> None:
        _stage_oscillation(wt)

        stop = _out(_converge(runner, wt, "status"))["stop"]

        assert stop["settle_payload"] == {
            "entries": [
                {
                    "severity": "MUST_FIX",
                    "file": _ENGINE_FILE,
                    "summary": _RECURRING,
                }
            ]
        }
        assert "fingerprint" not in json.dumps(stop)
