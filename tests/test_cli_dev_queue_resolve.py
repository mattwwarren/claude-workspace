"""CLI end-to-end: ``cw dev-queue resolve`` (#2702).

Records the operator's answers to a plan park on the row, requeues it through
``dev_queue_requeue`` (``ctx.invoke``) and prints the relay block. Every case
runs from an interactive (``headless: false``) session, because the suite may
itself run inside a headless dispatch worker.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import click
import pytest

from cw.cli.dev_queue._settle import SettleOutcome
from cw.codex_fix_loop.constraints import RESOLUTIONS_MARKER
from cw.dev_queue import load_dev_queue
from cw.events import read_events
from cw.exceptions import CwError
from cw.gh import AGENT_COMMENT_MARKER
from cw.models import OrchestratorEventType, QueueItemStatus, Stage
from cw.plan_fingerprint import compute_plan_draft_fingerprint
from tests._cli_review_helpers import _enter_operator_machine, _write_session_context
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    install_acme_client,
    invoke_dev_queue,
    make_blocked,
    patch_roster,
    seed_queue,
)
from tests.conftest import _write_project_config_yaml

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from click.testing import Result

    from cw.models import OrchestratorEvent, TicketTask

_FINGERPRINT = "b" * 64
_GATE: dict[str, object] = {
    "status": "plan_pending_approval",
    "plan_draft_fingerprint": _FINGERPRINT,
}
_AMBIGUITIES: dict[str, object] = {"status": "ambiguities_pending_resolution"}
_DECISIONS = "1. Use the row as the source of truth.\n2. Keep the lane."
_LINEAR_CONFIG = "tracking:\n  primary:\n    system: linear\n"
_GITHUB_CONFIG = "tracking:\n  primary:\n    system: github-issues\n"
_STILL_PARKED = "The row is still parked"
_APPROVAL_STILL_PARKED = "the approval is stamped and the row is still parked"
# The bound session's daemon surface: listing it in the roster makes the
# session live, so the composed requeue refuses.
_SURFACE = "abcd1234"


@pytest.fixture(autouse=True)
def _operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_acme_client(tmp_path, monkeypatch)
    _enter_operator_machine(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "cw.dev_queue.lifecycle.fetch_approved_plan_comment", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "cw.dev_queue.approval.fetch_issue_body", lambda *_a, **_k: None
    )


@pytest.fixture
def decisions(tmp_path: Path) -> Path:
    path = tmp_path / "decisions.md"
    path.write_text(f"{_DECISIONS}\n", encoding="utf-8")
    return path


def _seed(
    last_result: dict[str, object] | None = None, **overrides: object
) -> TicketTask:
    kwargs: dict[str, object] = {"stage": Stage.PLAN, "lane": "fast", "priority": 7}
    kwargs.update(overrides)
    task = make_blocked(**kwargs)
    session = bound_session_for(
        task, last_result=last_result or _AMBIGUITIES, surface_ref=_SURFACE
    )
    seed_queue([task], [session])
    return task


def _resolve(path: Path, *extra: str) -> Result:
    return invoke_dev_queue(
        "resolve", ACME_TICKET, "-c", ACME, "--file", str(path), *extra
    )


def _row() -> TicketTask:
    return next(t for t in load_dev_queue().tasks if t.ticket_id == ACME_TICKET)


def _events(event_type: OrchestratorEventType) -> list[OrchestratorEvent]:
    return list(read_events(event_types=[event_type]))


def _resolution_events() -> list[OrchestratorEvent]:
    return _events(OrchestratorEventType.OPERATOR_RESOLUTION_RECORDED)


def _record_posts(
    monkeypatch: pytest.MonkeyPatch, returncode: int | None = 0
) -> list[tuple[str, str, dict[str, object]]]:
    calls: list[tuple[str, str, dict[str, object]]] = []

    def _fake(
        ticket_id: str, body: str, **kwargs: object
    ) -> subprocess.CompletedProcess[bytes] | None:
        calls.append((ticket_id, body, kwargs))
        if returncode is None:
            return None
        return subprocess.CompletedProcess(args=[], returncode=returncode)

    monkeypatch.setattr("cw.cli.dev_queue.resolve.post_issue_comment", _fake)
    return calls


def _no_gh(*_a: object, **_k: object) -> None:
    msg = "gh must not be called"
    raise AssertionError(msg)


class TestHappyPath:
    def test_records_then_requeues_keeping_lane_and_priority(
        self, decisions: Path
    ) -> None:
        _seed()
        result = _resolve(decisions)
        assert result.exit_code == 0, result.output
        assert (
            f"Recorded resolution round 1 for {ACME_TICKET} ({ACME})" in result.stdout
        )
        assert (
            f"Requeued {ACME_TICKET} ({ACME}): plan -> plan (PENDING)" in result.stdout
        )
        assert "ticket" in result.stdout
        row = _row()
        assert row.status == QueueItemStatus.PENDING
        assert row.stage == Stage.PLAN
        assert row.lane == "fast"
        assert row.priority == 7
        assert [e.text for e in row.operator_resolutions] == [_DECISIONS]
        assert len(_resolution_events()) == 1
        assert len(_events(OrchestratorEventType.TICKET_REQUEUED)) == 1

    def test_relay_block_carries_both_markers_in_order(self, decisions: Path) -> None:
        _seed()
        result = _resolve(decisions)
        assert result.exit_code == 0, result.output
        out = result.stdout
        assert out.count(RESOLUTIONS_MARKER) == 1
        assert out.count(AGENT_COMMENT_MARKER) == 1
        decisions_at = out.index(_DECISIONS)
        assert decisions_at < out.index(RESOLUTIONS_MARKER)
        assert out.index(RESOLUTIONS_MARKER) < out.index(AGENT_COMMENT_MARKER)
        assert out.rstrip().endswith(AGENT_COMMENT_MARKER)
        assert "Operator resolutions (round 1, recorded via cw)" in out

    def test_draft_is_never_touched(self, tmp_path: Path, decisions: Path) -> None:
        worktree = tmp_path / "wt"
        draft = worktree / ".cw" / "plan-draft.md"
        draft.parent.mkdir(parents=True)
        draft.write_text("# Plan\n\nBody.\n", encoding="utf-8")
        before = draft.read_bytes()
        fingerprint = compute_plan_draft_fingerprint(draft.read_text(encoding="utf-8"))
        _seed(_GATE, worktree_path=worktree)
        result = _resolve(decisions, "--approve")
        assert result.exit_code == 0, result.output
        assert draft.read_bytes() == before
        assert (
            compute_plan_draft_fingerprint(draft.read_text(encoding="utf-8"))
            == fingerprint
        )


class TestWorkerRefusal:
    def _assert_refused(self, result: Result) -> None:
        assert result.exit_code != 0
        assert "dispatch worker" in result.output
        assert _row().operator_resolutions == []
        assert _resolution_events() == []

    def test_headless_context_refuses(self, tmp_path: Path, decisions: Path) -> None:
        _seed()
        _write_session_context(tmp_path, headless=True)
        self._assert_refused(_resolve(decisions))

    def test_worker_tmpdir_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decisions: Path
    ) -> None:
        _seed()
        worker = tmp_path / "worker-wt"
        _write_session_context(worker, headless=True)
        scratch = worker / ".cw" / "tmp"
        scratch.mkdir(parents=True)
        monkeypatch.setenv("TMPDIR", str(scratch))
        self._assert_refused(_resolve(decisions))

    def test_context_lookup_happens_at_call_time(
        self, monkeypatch: pytest.MonkeyPatch, decisions: Path
    ) -> None:
        _seed()
        monkeypatch.setattr(
            "cw.cli._hook_io.find_cw_context", lambda _path: {"headless": True}
        )
        self._assert_refused(_resolve(decisions))


class TestLinear:
    @pytest.mark.parametrize(
        "extra", [(), ("--post",), ("--approve",), ("--approve", "--post")]
    )
    def test_linear_never_calls_gh(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        decisions: Path,
        extra: tuple[str, ...],
    ) -> None:
        _write_project_config_yaml(tmp_path / "ws", _LINEAR_CONFIG)
        _seed(_GATE)
        monkeypatch.setattr("cw.cli.dev_queue.resolve.post_issue_comment", _no_gh)
        monkeypatch.setattr("cw.gh.fetch_issue_comments", _no_gh)
        monkeypatch.setattr(
            "cw.dev_queue.lifecycle.fetch_approved_plan_comment", _no_gh
        )
        monkeypatch.setattr("cw.dev_queue.approval.fetch_issue_body", _no_gh)
        result = _resolve(decisions, *extra)
        assert result.exit_code == 0, result.output
        row = _row()
        assert row.status == QueueItemStatus.PENDING
        assert [e.text for e in row.operator_resolutions] == [_DECISIONS]
        if "--post" in extra:
            assert "not GitHub" in result.output


class TestPost:
    @pytest.mark.parametrize(
        "config", [None, _GITHUB_CONFIG], ids=["unresolved", "github"]
    )
    def test_posts_once_without_the_agent_marker(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        decisions: Path,
        config: str | None,
    ) -> None:
        if config is not None:
            _write_project_config_yaml(tmp_path / "ws", config)
        _seed()
        calls = _record_posts(monkeypatch)
        result = _resolve(decisions, "--post")
        assert result.exit_code == 0, result.output
        ((ticket_id, body, kwargs),) = calls
        assert ticket_id == ACME_TICKET
        assert _DECISIONS in body
        assert RESOLUTIONS_MARKER in body
        assert AGENT_COMMENT_MARKER not in body
        assert kwargs.get("operator_authored", False) is False

    @pytest.mark.parametrize("returncode", [None, 1], ids=["no-result", "nonzero"])
    def test_failed_post_warns_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, decisions: Path, returncode: int | None
    ) -> None:
        _seed()
        _record_posts(monkeypatch, returncode=returncode)
        result = _resolve(decisions, "--post")
        assert result.exit_code == 0, result.output
        assert "failed to post" in result.stderr
        assert len(_row().operator_resolutions) == 1

    def test_duplicate_rerun_skips_the_post(
        self, monkeypatch: pytest.MonkeyPatch, decisions: Path
    ) -> None:
        _seed()
        patch_roster(monkeypatch, {_SURFACE})
        calls = _record_posts(monkeypatch)
        first = _resolve(decisions, "--post")
        assert first.exit_code != 0
        assert len(calls) == 1
        patch_roster(monkeypatch, set())
        second = _resolve(decisions, "--post")
        assert second.exit_code == 0, second.output
        assert len(calls) == 1
        assert "Skipped --post: identical to the latest recorded entry" in second.output

    def test_no_post_flag_posts_nothing(
        self, monkeypatch: pytest.MonkeyPatch, decisions: Path
    ) -> None:
        _seed()
        monkeypatch.setattr("cw.cli.dev_queue.resolve.post_issue_comment", _no_gh)
        result = _resolve(decisions)
        assert result.exit_code == 0, result.output


class TestFileErrors:
    def test_missing_path_is_a_usage_error(self, tmp_path: Path) -> None:
        _seed()
        result = _resolve(tmp_path / "absent.md")
        assert result.exit_code == 2
        assert _row().operator_resolutions == []

    def test_non_utf8_file(self, tmp_path: Path) -> None:
        _seed()
        path = tmp_path / "latin1.md"
        path.write_bytes(b"caf\xe9\n")
        result = _resolve(path)
        assert result.exit_code == 1
        assert str(path) in result.output
        assert "UTF-8" in result.output
        assert _row().operator_resolutions == []

    def test_unreadable_file(
        self, monkeypatch: pytest.MonkeyPatch, decisions: Path
    ) -> None:
        _seed()
        real_read_text = type(decisions).read_text

        def _boom(
            self: Path, encoding: str | None = None, errors: str | None = None
        ) -> str:
            if self == decisions:
                msg = "permission denied"
                raise PermissionError(msg)
            return real_read_text(self, encoding=encoding, errors=errors)

        monkeypatch.setattr(type(decisions), "read_text", _boom)
        result = _resolve(decisions)
        assert result.exit_code == 1
        assert "could not read" in result.output
        assert _row().operator_resolutions == []

    @pytest.mark.parametrize("content", ["", "  \n\t\n"], ids=["empty", "whitespace"])
    def test_blank_file(self, tmp_path: Path, content: str) -> None:
        _seed()
        path = tmp_path / "blank.md"
        path.write_text(content, encoding="utf-8")
        result = _resolve(path)
        assert result.exit_code == 1
        assert "blank" in result.output
        assert _row().operator_resolutions == []

    def test_oversize_file(self, tmp_path: Path) -> None:
        _seed()
        path = tmp_path / "big.md"
        path.write_text("x" * (64 * 1024 + 1), encoding="utf-8")
        result = _resolve(path)
        assert result.exit_code == 1
        assert "65536" in result.output
        assert _row().operator_resolutions == []


class TestDuplicateEndToEnd:
    def test_refused_requeue_then_rerun_records_nothing_new(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decisions: Path
    ) -> None:
        _seed()
        patch_roster(monkeypatch, {_SURFACE})
        first = _resolve(decisions)
        assert first.exit_code != 0
        assert "Resolution round 1 is recorded" in first.output
        assert _STILL_PARKED in first.output
        assert f"cw dev-queue requeue {ACME_TICKET} -c {ACME}" in first.output
        assert _row().status == QueueItemStatus.BLOCKED_ON_USER

        patch_roster(monkeypatch, set())
        second = _resolve(decisions)
        assert second.exit_code == 0, second.output
        assert "already recorded" in second.output
        row = _row()
        assert row.status == QueueItemStatus.PENDING
        assert len(row.operator_resolutions) == 1
        assert len(_resolution_events()) == 1

    def test_different_file_appends_round_two(
        self, tmp_path: Path, decisions: Path
    ) -> None:
        _seed()
        assert _resolve(decisions).exit_code == 0
        # The plan stage parks the row again; the ledger survived the requeue.
        reparked = _row()
        assert len(reparked.operator_resolutions) == 1
        reparked.status = QueueItemStatus.BLOCKED_ON_USER
        reparked.session_id = "sess-1"
        seed_queue([reparked])
        second = tmp_path / "second.md"
        second.write_text("3. A new answer.\n", encoding="utf-8")
        result = _resolve(second)
        assert result.exit_code == 0, result.output
        assert "Recorded resolution round 2" in result.output
        assert [e.round for e in _row().operator_resolutions] == [1, 2]


class TestApprove:
    def test_approve_stamps_bound_and_requeues(self, decisions: Path) -> None:
        _seed(_GATE)
        result = _resolve(decisions, "--approve")
        assert result.exit_code == 0, result.output
        assert f"Plan approval stamped, bound to draft {_FINGERPRINT[:12]}." in (
            result.stdout
        )
        row = _row()
        assert row.plan_approved_fingerprint == _FINGERPRINT
        assert row.plan_approved_at is not None
        assert row.status == QueueItemStatus.PENDING
        assert row.stage == Stage.PLAN

    def test_approve_unbound(self, decisions: Path) -> None:
        _seed({"status": "plan_pending_approval"})
        result = _resolve(decisions, "--approve")
        assert result.exit_code == 0, result.output
        assert "unbound" in result.stdout
        assert _row().plan_approved_fingerprint is None
        assert _row().plan_approved_at is not None

    def test_approve_on_an_ambiguities_park_refuses(self, decisions: Path) -> None:
        _seed()
        result = _resolve(decisions, "--approve")
        assert result.exit_code != 0
        assert "without --approve" in result.output
        assert "re-park" in result.output
        assert "cw dev-queue approve" in result.output
        row = _row()
        assert row.operator_resolutions == []
        assert row.status == QueueItemStatus.BLOCKED_ON_USER

    def test_approve_without_a_session_refuses(self, decisions: Path) -> None:
        seed_queue([make_blocked(stage=Stage.PLAN, session_id=None)])
        result = _resolve(decisions, "--approve")
        assert result.exit_code != 0
        assert "session was not found" in result.output
        assert _row().operator_resolutions == []

    def test_settled_into_a_human_gate_refuses_only_with_approve(
        self, monkeypatch: pytest.MonkeyPatch, decisions: Path
    ) -> None:
        task = _seed(_GATE, scope_hint="large", disposition="approval_gate")
        monkeypatch.setattr(
            "cw.cli.dev_queue.resolve.settle_lagging_row",
            lambda *_a: SettleOutcome(task, routed=True),
        )
        refused = _resolve(decisions, "--approve")
        assert refused.exit_code != 0
        assert "human-kept gate" in refused.output
        assert _row().operator_resolutions == []

        proceeded = _resolve(decisions)
        assert [e.text for e in _row().operator_resolutions] == [_DECISIONS]
        assert "Recorded resolution round 1" in proceeded.output

    def test_requeue_failure_after_approve_says_stamped_and_parked(
        self, monkeypatch: pytest.MonkeyPatch, decisions: Path
    ) -> None:
        _seed(_GATE)
        patch_roster(monkeypatch, {_SURFACE})
        result = _resolve(decisions, "--approve")
        assert result.exit_code != 0
        assert _APPROVAL_STILL_PARKED in result.output
        assert _row().plan_approved_fingerprint == _FINGERPRINT

    @pytest.mark.parametrize("reviewed", [True, False])
    def test_plan_reviewed_note(
        self, monkeypatch: pytest.MonkeyPatch, decisions: Path, reviewed: bool
    ) -> None:
        _seed(_GATE)
        monkeypatch.setattr(
            "cw.dev_queue.operator_resolutions._plan_is_reviewed",
            lambda *_a: reviewed,
        )
        result = _resolve(decisions, "--approve")
        assert result.exit_code == 0, result.output
        assert ("not yet quality-reviewed (#968)" in result.output) is not reviewed
        row = _row()
        assert row.status == QueueItemStatus.PENDING
        assert row.stage == Stage.PLAN

    def test_body_drift_warning_goes_to_stderr(
        self, monkeypatch: pytest.MonkeyPatch, decisions: Path
    ) -> None:
        _seed(_GATE)
        monkeypatch.setattr(
            "cw.dev_queue.approval._read_persisted_body_sha", lambda *_a: "c" * 64
        )
        monkeypatch.setattr(
            "cw.dev_queue.approval.fetch_issue_body", lambda *_a, **_k: "edited\n"
        )
        result = _resolve(decisions, "--approve")
        assert result.exit_code == 0, result.output
        assert "changed after the plan stage last evaluated it" in result.stderr


def _exit_with_sys_exit(**_kwargs: object) -> None:
    sys.exit(2)


def _exit_with_ctx_exit(**_kwargs: object) -> None:
    click.get_current_context().exit(3)


def _exit_zero(**_kwargs: object) -> None:
    sys.exit(0)


class TestInvokeWrap:
    @pytest.mark.parametrize(
        ("callback", "code"),
        [(_exit_with_sys_exit, 2), (_exit_with_ctx_exit, 3)],
        ids=["sys-exit", "ctx-exit"],
    )
    def test_non_click_exception_exits_keep_code_and_note(
        self,
        monkeypatch: pytest.MonkeyPatch,
        decisions: Path,
        callback: Callable[..., None],
        code: int,
    ) -> None:
        _seed()
        monkeypatch.setattr(
            "cw.cli.dev_queue.resolve.dev_queue_requeue",
            click.Command("requeue", callback=callback),
        )
        result = _resolve(decisions)
        assert result.exit_code == code
        assert "Resolution round 1 is recorded" in result.stderr
        assert _STILL_PARKED in result.stderr

    def test_zero_exit_passes_through(
        self, monkeypatch: pytest.MonkeyPatch, decisions: Path
    ) -> None:
        _seed()
        monkeypatch.setattr(
            "cw.cli.dev_queue.resolve.dev_queue_requeue",
            click.Command("requeue", callback=_exit_zero),
        )
        result = _resolve(decisions)
        assert result.exit_code == 0, result.output
        assert "is recorded, but the requeue failed" not in result.output

    def test_unwrapped_cw_error_becomes_a_click_error_with_the_note(
        self, monkeypatch: pytest.MonkeyPatch, decisions: Path
    ) -> None:
        """A requeue callback that raises ``CwError`` past ``handle_errors``."""
        _seed()

        def _raise_cw_error(**_kwargs: object) -> None:
            msg = "requeue backend unavailable"
            raise CwError(msg)

        monkeypatch.setattr(
            "cw.cli.dev_queue.resolve.dev_queue_requeue",
            click.Command("requeue", callback=_raise_cw_error),
        )
        result = _resolve(decisions)
        assert result.exit_code == 1
        assert "requeue backend unavailable" in result.stderr
        assert "Resolution round 1 is recorded" in result.stderr
