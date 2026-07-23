"""Tests for cw.result — validate_payload helper and the emit command."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.config import load_state, save_state, sessions_lock
from cw.exceptions import EmitSessionNotFoundError, EmitValidationError
from cw.result import EmitOutcome, emit_result, emit_result_locked, validate_payload
from tests.conftest import _seed_daemon_session

if TYPE_CHECKING:
    from pathlib import Path


def _valid_payload() -> dict[str, Any]:
    """Minimal valid shipped payload for testing."""
    return {
        "schema_version": 1,
        "ticket_id": "GEN-1234",
        "status": "shipped",
        "stage_reached": "stage5_post_create",
        "scope": {
            "tier": "small",
            "files": 3,
            "lines_estimate": 42,
            "lines_actual": 47,
            "forbidden_touched": False,
        },
        "plan_source": "linear_existing",
        "branch": "dev/gen-1234-fix-login",
        "worktree_path": "/tmp/wt/gen-1234",
        "fork_point_sha": "abc1234",
        "commits": ["sha1", "sha2"],
        "pr": {
            "number": 42,
            "url": "https://github.com/foo/bar/pull/42",
            "auto_merge": True,
            "base": "main",
        },
        "review": {"must_fix_initial": 0, "should_fix": 1, "fix_cycles_used": 0},
        "health": {
            "lowest_agent_confidence": "MEDIUM",
            "any_incomplete_risk": False,
            "shortcuts": [],
            "recommendation": "PROCEED",
            "downgrade_applied": False,
            "fix_loop_escalated": False,
        },
        "friction_highlights": [],
        "blocker": None,
        "next_actions": ["wait_for_ci"],
    }


class TestValidatePayload:
    def test_valid_shipped_payload_returns_no_errors(self) -> None:
        errors = validate_payload(_valid_payload())
        assert errors == []

    def test_pr_non_null_with_blocked_status_returns_error(self) -> None:
        payload = _valid_payload()
        payload["status"] = "blocked"
        payload["pr"] = {"number": 1, "url": "...", "auto_merge": True, "base": "main"}
        payload["blocker"] = {"stage": "s2", "reason": "impl_failed", "details": "x"}
        payload["next_actions"] = []
        errors = validate_payload(payload)
        assert any("pr" in e for e in errors)

    def test_bad_stage_reached_returns_error(self) -> None:
        payload = _valid_payload()
        payload["stage_reached"] = "not_a_real_stage"
        errors = validate_payload(payload)
        assert len(errors) > 0

    def test_lines_actual_non_null_at_stage1_plan_returns_error(self) -> None:
        payload = _valid_payload()
        payload["status"] = "plan_pending_approval"
        payload["stage_reached"] = "stage1_plan"
        payload["scope"]["tier"] = "large"
        payload["scope"]["lines_actual"] = 99  # should be null at stage1_plan
        payload["branch"] = None
        payload["worktree_path"] = None
        payload["fork_point_sha"] = None
        payload["commits"] = []
        payload["pr"] = None
        payload["health"]["lowest_agent_confidence"] = "HIGH"
        payload["next_actions"] = []
        errors = validate_payload(payload)
        assert any("lines_actual" in e for e in errors)


class TestEmitResultLocked:
    """Direct-call tests for ``emit_result_locked`` (RFC 0012 S1, #1455).

    Mirrors ``TestValidatePayload``'s direct-call style (no ``CliRunner``).
    Every call is made from inside an already-held ``sessions_lock()`` block,
    per the "caller MUST already hold the lock" contract.
    """

    def test_records_result_and_returns_outcome(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        with sessions_lock():
            outcome = emit_result_locked(_valid_payload(), "test1234")

        assert isinstance(outcome, EmitOutcome)
        assert outcome.session_id == "test1234"
        assert outcome.result.status == "shipped"
        assert outcome.prior_status is None

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is not None
        assert sess.last_result["status"] == "shipped"

    def test_prior_status_captured_when_result_already_present(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        with sessions_lock():
            state = load_state()
            sess = next(s for s in state.sessions if s.id == "test1234")
            sess.last_result = {"status": "blocked"}
            save_state(state)

            outcome = emit_result_locked(_valid_payload(), "test1234")

        assert outcome.prior_status == "blocked"

    def test_validation_failure_raises_and_carries_errors(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        payload = _valid_payload()
        payload["pr"] = None  # shipped requires non-null pr -> cross-field error

        with sessions_lock(), pytest.raises(EmitValidationError) as exc_info:
            emit_result_locked(payload, "test1234")

        assert any("pr must be non-null" in line for line in exc_info.value.errors)

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is None

    def test_session_not_found_raises_and_carries_session_id(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")

        with sessions_lock(), pytest.raises(EmitSessionNotFoundError) as exc_info:
            emit_result_locked(_valid_payload(), "nosuch99")

        assert exc_info.value.session_id == "nosuch99"


class TestEmitResult:
    """Direct-call tests for ``emit_result``, the unlocked wrapper.

    No ambient ``sessions_lock()`` is held here -- ``emit_result`` acquires
    the lock itself, mirroring ``cw.dev_queue.approval.approve_ticket``.
    """

    def test_acquires_lock_and_delegates(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        outcome = emit_result(_valid_payload(), "test1234")

        assert outcome.session_id == "test1234"
        assert outcome.result.status == "shipped"
        assert outcome.prior_status is None

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is not None
        assert sess.last_result["status"] == "shipped"

    def test_propagates_validation_error(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        payload = _valid_payload()
        payload["pr"] = None

        with pytest.raises(EmitValidationError) as exc_info:
            emit_result(payload, "test1234")

        assert any("pr must be non-null" in line for line in exc_info.value.errors)

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is None

    def test_propagates_session_not_found_error(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")

        with pytest.raises(EmitSessionNotFoundError) as exc_info:
            emit_result(_valid_payload(), "nosuch99")

        assert exc_info.value.session_id == "nosuch99"


class TestResultEmit:
    """Tests for ``cw result emit`` (push-based completion, #536 Phase 1).

    The sibling ``TestResultValidate`` CLI precedent lives in ``test_cli.py``;
    emit mirrors validate's I/O shape (positional PATH, ``-`` stdin, json:
    prefixed decode errors, ``field: message`` validation lines).
    """

    def test_happy_path_session_id_override(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["result", "emit", "-", "--session-id", "test1234"],
            input=json.dumps(_valid_payload()),
        )
        assert result.exit_code == 0, result.output
        assert result.output == "Recorded result for session test1234: status=shipped\n"

        state = load_state()
        sess = next(s for s in state.sessions if s.id == "test1234")
        assert sess.last_result is not None
        assert sess.last_result["status"] == "shipped"
        # Write-only: emit records the result but does NOT complete the session.
        assert sess.status.value == "active"

    def test_validation_failure_no_mutation(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        payload = _valid_payload()
        payload["pr"] = None  # shipped requires non-null pr → cross-field error
        result = CliRunner().invoke(
            main,
            ["result", "emit", "-", "--session-id", "test1234"],
            input=json.dumps(payload),
        )
        assert result.exit_code == 1
        # field: message line(s) from _format_errors, plus the no-mutation notice.
        # Field-specific (not just "any colon-containing line") so a regression
        # that silently drops the real pr/status cross-field error is caught.
        assert any("pr must be non-null" in line for line in result.output.splitlines())
        assert "No session state was modified." in result.output

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is None

    def test_missing_cw_context_is_loud_error(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        no_context = tmp_path / "no-context"
        no_context.mkdir()
        monkeypatch.chdir(no_context)
        result = CliRunner().invoke(
            main, ["result", "emit", "-"], input=json.dumps(_valid_payload())
        )
        assert result.exit_code == 1
        assert ".claude/cw-context.json" in result.output

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is None

    def test_context_file_resolution(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        worktree = tmp_path / "wt"
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "cw-context.json").write_text(
            json.dumps({"session_id": "test1234"})
        )
        monkeypatch.chdir(worktree)
        result = CliRunner().invoke(
            main, ["result", "emit", "-"], input=json.dumps(_valid_payload())
        )
        assert result.exit_code == 0, result.output

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is not None
        assert sess.last_result["status"] == "shipped"

    def test_session_id_flag_wins_over_context(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        worktree = tmp_path / "wt"
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True)
        # Context names a DIFFERENT (unseeded) session; the flag must override it.
        (claude_dir / "cw-context.json").write_text(
            json.dumps({"session_id": "other999"})
        )
        monkeypatch.chdir(worktree)
        result = CliRunner().invoke(
            main,
            ["result", "emit", "-", "--session-id", "test1234"],
            input=json.dumps(_valid_payload()),
        )
        assert result.exit_code == 0, result.output

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is not None
        assert sess.last_result["status"] == "shipped"

    def test_path_argument_parity(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps(_valid_payload()))
        result = CliRunner().invoke(
            main,
            ["result", "emit", str(payload_file), "--session-id", "test1234"],
        )
        assert result.exit_code == 0, result.output

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is not None
        assert sess.last_result["status"] == "shipped"

    def test_malformed_json_no_mutation(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        result = CliRunner().invoke(
            main,
            ["result", "emit", "-", "--session-id", "test1234"],
            input="{not valid json",
        )
        assert result.exit_code == 1
        assert result.output.startswith("json:")

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is None

    def test_context_without_string_session_id_is_loud_error(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        worktree = tmp_path / "wt"
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True)
        # session_id present but not a string → loud error, no fallback.
        (claude_dir / "cw-context.json").write_text(json.dumps({"session_id": 42}))
        monkeypatch.chdir(worktree)
        result = CliRunner().invoke(
            main, ["result", "emit", "-"], input=json.dumps(_valid_payload())
        )
        assert result.exit_code == 1
        assert "no string session_id" in result.output

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is None

    def test_unknown_session_id_is_loud_error(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        result = CliRunner().invoke(
            main,
            ["result", "emit", "-", "--session-id", "nosuch99"],
            input=json.dumps(_valid_payload()),
        )
        assert result.exit_code == 1
        assert "not found" in result.output

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is None
