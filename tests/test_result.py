"""Tests for cw.result — validate_payload helper and the emit command."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cw.auto_dev_result import AutoDevResult, BlockedResult
from cw.cli import main
from cw.config import load_state, save_state, sessions_lock
from cw.exceptions import EmitSessionNotFoundError, EmitValidationError
from cw.models import LastResultSource, Session, SessionPurpose
from cw.result import (
    EmitOutcome,
    emit_result,
    emit_result_locked,
    emit_result_on,
    has_terminal_result,
    validate_payload,
)
from tests.conftest import _seed_daemon_session


def _in_memory_session(**overrides: Any) -> Session:
    """Construct a bare in-memory Session for pure emit_result_on tests.

    No state file is touched -- emit_result_on performs zero I/O, so these
    tests never go through load_state/save_state.
    """
    kwargs: dict[str, Any] = {
        "id": "sess1234",
        "name": "acme/impl",
        "client": "acme",
        "purpose": SessionPurpose.IMPL,
        "workspace_path": Path("/tmp/acme"),
    }
    kwargs.update(overrides)
    return Session(**kwargs)


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


class TestEmitResultOn:
    """Pure-mutator tests for ``emit_result_on`` (RFC 0012 A3, #1459).

    No ``sessions_lock``/``load_state``/``save_state`` -- the function performs
    zero I/O and mutates the passed-in ``Session`` in place.
    """

    def test_mutates_session_in_place_and_returns_outcome(self) -> None:
        session = _in_memory_session()
        outcome = emit_result_on(
            session, _valid_payload(), source=LastResultSource.GIT_SYNTHESIS
        )

        assert isinstance(outcome, EmitOutcome)
        assert outcome.refused is False
        assert outcome.result is not None
        assert outcome.result.status == "shipped"
        assert outcome.prior_status is None
        assert outcome.session_id == "sess1234"
        # Mutated in place.
        assert session.last_result is not None
        assert session.last_result["status"] == "shipped"
        assert session.last_result_source == LastResultSource.GIT_SYNTHESIS

    def test_refusal_leaves_session_byte_identical(self) -> None:
        foreign = {"status": "blocked", "totally_unknown": {"x": 1}}
        session = _in_memory_session(
            last_result=foreign,
            last_result_source=LastResultSource.STOP_HOOK_HARVEST,
        )
        before = session.model_dump(mode="json")

        outcome = emit_result_on(
            session, _valid_payload(), source=LastResultSource.SALVAGE_TRANSCRIPT
        )

        assert outcome.refused is True
        assert outcome.result is None
        assert outcome.prior_status == "blocked"
        assert outcome.existing_result == foreign
        assert outcome.existing_source == LastResultSource.STOP_HOOK_HARVEST
        # Session left completely untouched.
        assert session.model_dump(mode="json") == before

    def test_validation_error_raises_before_mutation(self) -> None:
        session = _in_memory_session()
        payload = _valid_payload()
        payload["pr"] = None  # shipped requires non-null pr -> cross-field error

        with pytest.raises(EmitValidationError):
            emit_result_on(session, payload, source=LastResultSource.GIT_SYNTHESIS)

        assert session.last_result is None
        assert session.last_result_source is None


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
            outcome = emit_result_locked(
                _valid_payload(), "test1234", source=LastResultSource.EMIT_CLI
            )

        assert isinstance(outcome, EmitOutcome)
        assert outcome.session_id == "test1234"
        assert outcome.refused is False
        assert outcome.result is not None
        assert outcome.result.status == "shipped"
        assert outcome.prior_status is None

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is not None
        assert sess.last_result["status"] == "shipped"
        assert sess.last_result_source == LastResultSource.EMIT_CLI

    def test_performs_exactly_one_load_and_one_save(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The thin I/O wrapper does one load_state and one save_state on an
        accepted write (RFC 0012 A3 #1459 -- signature/behavior unchanged)."""
        import cw.result as result_mod

        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        load_calls = 0
        save_calls = 0
        real_load = result_mod.load_state
        real_save = result_mod.save_state

        def _spy_load() -> Any:
            nonlocal load_calls
            load_calls += 1
            return real_load()

        def _spy_save(state: Any) -> None:
            nonlocal save_calls
            save_calls += 1
            real_save(state)

        monkeypatch.setattr(result_mod, "load_state", _spy_load)
        monkeypatch.setattr(result_mod, "save_state", _spy_save)

        with sessions_lock():
            emit_result_locked(
                _valid_payload(), "test1234", source=LastResultSource.EMIT_CLI
            )

        assert load_calls == 1
        assert save_calls == 1

    def test_refusal_skips_save_state(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A refused write mutates nothing, so no save_state is issued."""
        import cw.result as result_mod

        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="test1234",
            last_result={"status": "shipped"},
        )
        save_calls = 0
        real_save = result_mod.save_state

        def _spy_save(state: Any) -> None:
            nonlocal save_calls
            save_calls += 1
            real_save(state)

        monkeypatch.setattr(result_mod, "save_state", _spy_save)

        with sessions_lock():
            outcome = emit_result_locked(
                _valid_payload(), "test1234", source=LastResultSource.EMIT_CLI
            )

        assert outcome.refused is True
        assert save_calls == 0

    def test_prior_status_captured_when_result_already_present(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A terminal last_result is refused, not overwritten (RFC 0012 S2)."""
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        with sessions_lock():
            state = load_state()
            sess = next(s for s in state.sessions if s.id == "test1234")
            sess.last_result = {"status": "blocked"}
            save_state(state)

            outcome = emit_result_locked(
                _valid_payload(), "test1234", source=LastResultSource.EMIT_CLI
            )

        assert outcome.refused is True
        assert outcome.result is None
        assert outcome.prior_status == "blocked"
        assert outcome.existing_result == {"status": "blocked"}
        assert outcome.existing_source is None

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result == {"status": "blocked"}

    def test_validation_failure_raises_and_carries_errors(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        payload = _valid_payload()
        payload["pr"] = None  # shipped requires non-null pr -> cross-field error

        with sessions_lock(), pytest.raises(EmitValidationError) as exc_info:
            emit_result_locked(payload, "test1234", source=LastResultSource.EMIT_CLI)

        assert any("pr must be non-null" in line for line in exc_info.value.errors)

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is None

    def test_session_not_found_raises_and_carries_session_id(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")

        with sessions_lock(), pytest.raises(EmitSessionNotFoundError) as exc_info:
            emit_result_locked(
                _valid_payload(), "nosuch99", source=LastResultSource.EMIT_CLI
            )

        assert exc_info.value.session_id == "nosuch99"

    def test_emit_result_locked_refuses_second_write_and_logs_collision(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="test1234",
            last_result={"status": "shipped"},
            last_result_source=LastResultSource.STOP_HOOK_HARVEST,
        )
        with caplog.at_level("WARNING"), sessions_lock():
            outcome = emit_result_locked(
                _valid_payload(), "test1234", source=LastResultSource.EMIT_CLI
            )

        assert outcome.refused is True
        assert outcome.existing_source == LastResultSource.STOP_HOOK_HARVEST
        assert outcome.existing_result is not None
        assert outcome.existing_result["status"] == "shipped"

        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "test1234" in message
        assert "stop_hook_harvest" in message
        assert "emit_cli" in message
        assert "shipped" in message

    def test_emit_result_locked_refusal_does_not_validate_foreign_shape(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        foreign_result = {"status": "blocked", "totally_unknown_field": {"x": 1}}
        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="test1234",
            last_result=foreign_result,
        )
        with sessions_lock():
            outcome = emit_result_locked(
                _valid_payload(), "test1234", source=LastResultSource.EMIT_CLI
            )

        assert outcome.refused is True
        assert outcome.existing_result == foreign_result

    def test_emit_result_locked_writes_over_non_terminal_park_marker(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="test1234",
            last_result={"paused_status": "silently_idle"},
        )
        with sessions_lock():
            outcome = emit_result_locked(
                _valid_payload(), "test1234", source=LastResultSource.GIT_SYNTHESIS
            )

        assert outcome.refused is False

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result_source == LastResultSource.GIT_SYNTHESIS

    def test_emit_result_locked_stamps_source_on_first_write(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        # last_result defaults to None per the Session model -- no override.
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        with sessions_lock():
            outcome = emit_result_locked(
                _valid_payload(), "test1234", source=LastResultSource.EXECUTOR_DIRECT
            )

        assert outcome.refused is False

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result_source == LastResultSource.EXECUTOR_DIRECT

    def test_emit_result_locked_accepts_blocked_result_shape_and_stamps_source(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """RFC 0012 A1 (#1457): the door widens to accept a parser-synthesized
        ``BlockedResult`` shape (no ``schema_version``, no full AutoDevResult
        fields) -- the shape the Stop-hook harvest writes now route through."""
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        blocked_payload = {
            "status": "blocked",
            "blocker": {"stage": "s1", "reason": "validation_failed", "details": "x"},
        }
        with sessions_lock():
            outcome = emit_result_locked(
                blocked_payload, "test1234", source=LastResultSource.STOP_HOOK_HARVEST
            )

        assert outcome.refused is False
        assert isinstance(outcome.result, BlockedResult)

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result_source == LastResultSource.STOP_HOOK_HARVEST

    def test_emit_result_locked_rejects_foreign_blocked_shape_missing_blocker(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A bare ``{"status": "blocked"}`` (no ``blocker``, no
        ``schema_version``) matches neither model -- the discriminant picks
        ``BlockedResult`` (no schema_version) but it still fails validation
        on the missing required ``blocker`` field."""
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        with (
            sessions_lock(),
            pytest.raises(EmitValidationError) as exc_info,
        ):
            emit_result_locked(
                {"status": "blocked"},
                "test1234",
                source=LastResultSource.STOP_HOOK_HARVEST,
            )

        assert any("blocker" in line for line in exc_info.value.errors)

    def test_emit_result_locked_full_blocked_autodev_result_stays_autodev_result(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A full producer-emitted AutoDevResult with status=blocked (carries
        ``schema_version``) must NOT be misrouted to the ``BlockedResult``
        branch -- the discriminant keys off ``schema_version`` presence."""
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        payload = _valid_payload()
        payload["status"] = "blocked"
        payload["pr"] = None
        payload["next_actions"] = []
        payload["blocker"] = {"stage": "s2", "reason": "impl_failed", "details": "x"}
        with sessions_lock():
            outcome = emit_result_locked(
                payload, "test1234", source=LastResultSource.STOP_HOOK_HARVEST
            )

        assert outcome.refused is False
        assert isinstance(outcome.result, AutoDevResult)


class TestEmitResult:
    """Direct-call tests for ``emit_result``, the unlocked wrapper.

    No ambient ``sessions_lock()`` is held here -- ``emit_result`` acquires
    the lock itself, mirroring ``cw.dev_queue.approval.approve_ticket``.
    """

    def test_acquires_lock_and_delegates(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        outcome = emit_result(
            _valid_payload(), "test1234", source=LastResultSource.EMIT_CLI
        )

        assert outcome.session_id == "test1234"
        assert outcome.refused is False
        assert outcome.result is not None
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
            emit_result(payload, "test1234", source=LastResultSource.EMIT_CLI)

        assert any("pr must be non-null" in line for line in exc_info.value.errors)

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is None

    def test_propagates_session_not_found_error(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")

        with pytest.raises(EmitSessionNotFoundError) as exc_info:
            emit_result(_valid_payload(), "nosuch99", source=LastResultSource.EMIT_CLI)

        assert exc_info.value.session_id == "nosuch99"

    def test_emit_result_forwards_source_to_locked(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Mirrors test_acquires_lock_and_delegates re: refusal + stamping."""
        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="test1234",
            last_result={"status": "shipped"},
            last_result_source=LastResultSource.STOP_HOOK_HARVEST,
        )
        outcome = emit_result(
            _valid_payload(), "test1234", source=LastResultSource.EMIT_CLI
        )
        assert outcome.refused is True
        assert outcome.existing_source == LastResultSource.STOP_HOOK_HARVEST

        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="fresh999")
        outcome2 = emit_result(
            _valid_payload(), "fresh999", source=LastResultSource.EXECUTOR_DIRECT
        )
        assert outcome2.refused is False
        sess = next(s for s in load_state().sessions if s.id == "fresh999")
        assert sess.last_result_source == LastResultSource.EXECUTOR_DIRECT


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

    def test_result_emit_cli_refusal_exit_zero_pinned_message(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="test1234",
            last_result={"status": "blocked"},
            last_result_source=LastResultSource.SALVAGE_TRANSCRIPT,
        )
        result = CliRunner().invoke(
            main,
            ["result", "emit", "-", "--session-id", "test1234"],
            input=json.dumps(_valid_payload()),
        )
        assert result.exit_code == 0, result.output
        assert result.output == (
            "Result already recorded for session test1234 "
            "(source=salvage_transcript); not overwritten.\n"
        )

        state = load_state()
        sess = next(s for s in state.sessions if s.id == "test1234")
        assert sess.last_result == {"status": "blocked"}
        assert sess.last_result_source == LastResultSource.SALVAGE_TRANSCRIPT

    def test_result_emit_cli_rejects_bare_blocked_shape_payload(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """CLI byte-compat (RFC 0012 A1, #1457): the widened door would
        accept a bare ``{"status": "blocked", "blocker": {...}}`` shape (no
        ``schema_version``), but ``cw result emit``'s strict pre-check
        (``_validate_or_exit``, AutoDevResult-only) still rejects it -- the
        widening is Stop-hook-harvest-only, not a CLI contract change."""
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        blocked_payload = {
            "status": "blocked",
            "blocker": {"stage": "s1", "reason": "validation_failed", "details": "x"},
        }
        result = CliRunner().invoke(
            main,
            ["result", "emit", "-", "--session-id", "test1234"],
            input=json.dumps(blocked_payload),
        )
        assert result.exit_code == 1
        assert result.output.strip() != ""

        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is None


class TestHasTerminalResult:
    """cw.result.has_terminal_result -- the door's terminal-ness predicate
    (RFC 0012 S2, #1456), and its delegation from reconcile/_shared."""

    def test_has_terminal_result_predicate_shapes(self) -> None:
        assert has_terminal_result({"status": "shipped"}) is True
        assert has_terminal_result({"paused_status": "silently_idle"}) is False
        assert has_terminal_result(None) is False

    def test_has_terminal_sentinel_delegates_to_door_predicate(self) -> None:
        from cw.models import Session, SessionOrigin, SessionPurpose, SessionStatus
        from cw.reconcile._shared import _has_terminal_sentinel

        def make_session(last_result: dict[str, Any] | None) -> Session:
            return Session(
                name="acme/impl",
                client="acme",
                purpose=SessionPurpose.IMPL,
                origin=SessionOrigin.DAEMON,
                status=SessionStatus.ACTIVE,
                workspace_path=Path("/tmp/acme"),
                last_result=last_result,
            )

        for shape in ({"status": "shipped"}, {"paused_status": "x"}, None):
            session = make_session(shape)
            assert _has_terminal_sentinel(session) == has_terminal_result(shape)
