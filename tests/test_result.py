"""Tests for cw.result — validate_payload helper and the emit command."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cw.auto_dev_result import AutoDevResult, BlockedResult
from cw.cli import main
from cw.config import load_state, save_state, sessions_lock
from cw.exceptions import EmitSessionNotFoundError, EmitValidationError
from cw.models import CwState, LastResultSource, Session, SessionPurpose
from cw.plan_fingerprint import compute_plan_draft_fingerprint
from cw.result import (
    EmitOutcome,
    emit_result,
    emit_result_locked,
    emit_result_on,
    has_terminal_result,
    validate_payload,
)
from tests.conftest import _plan_pending_payload, _seed_daemon_session


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

    def test_result_emit_ambiguous_session_id_reports_candidates(
        self, tmp_config_dir: Path
    ) -> None:
        """#2237: an ambiguous --session-id name lists candidates, writes nothing."""
        name = "acme/auto-dev/GEN-1234"
        save_state(
            CwState(
                sessions=[
                    _in_memory_session(id=sid, name=name)
                    for sid in ("ambig001", "ambig002")
                ]
            )
        )

        result = CliRunner().invoke(
            main,
            ["result", "emit", "-", "--session-id", name],
            input=json.dumps(_valid_payload()),
        )

        assert result.exit_code == 1
        assert "ambig001" in result.output
        assert "ambig002" in result.output
        assert "pass an id to choose" in result.output
        assert "No session state was modified." in result.output
        assert all(s.last_result is None for s in load_state().sessions)

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

    def test_result_emit_cli_locked_refusal_after_pre_check_passed(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2382: the read-only pre-check saw no terminal result, but the
        locked door refused (a concurrent writer landed first). The CLI
        reports the door's refusal, exit 0, and prints no binding note."""
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")

        def _refuse(
            payload: dict[str, Any], session_id: str, *, source: LastResultSource
        ) -> EmitOutcome:
            return EmitOutcome(
                session_id=session_id,
                result=None,
                prior_status="shipped",
                refused=True,
                existing_result={"status": "shipped"},
                existing_source=LastResultSource.STOP_HOOK_HARVEST,
            )

        monkeypatch.setattr("cw.result.emit_result", _refuse)
        result = CliRunner().invoke(
            main,
            ["result", "emit", "-", "--session-id", "test1234"],
            input=json.dumps(_valid_payload()),
        )
        assert result.exit_code == 0, result.output
        assert result.output == (
            "Result already recorded for session test1234 "
            "(source=stop_hook_harvest); not overwritten.\n"
        )

    def test_result_emit_cli_renders_door_validation_error(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The door's own validation arm (reached only if it ever disagrees
        with the CLI's strict pre-validation) renders field lines plus the
        no-mutation notice."""
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")

        def _reject(
            payload: dict[str, Any], session_id: str, *, source: LastResultSource
        ) -> EmitOutcome:
            msg = "door rejected the payload"
            raise EmitValidationError(msg, errors=["pr: door said no"])

        monkeypatch.setattr("cw.result.emit_result", _reject)
        result = CliRunner().invoke(
            main,
            ["result", "emit", "-", "--session-id", "test1234"],
            input=json.dumps(_valid_payload()),
        )
        assert result.exit_code == 1
        assert "pr: door said no" in result.output
        assert "No session state was modified." in result.output

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


def _claim(fingerprint: object) -> dict[str, Any]:
    """A v8 plan_pending payload carrying *fingerprint* as its claim."""
    return _plan_pending_payload(
        schema_version=8, ticket_id="GEN-2382", plan_draft_fingerprint=fingerprint
    )


_DRAFT_TEXT = "<!-- plan-stage-scan-round: 1 -->\n# Plan\n\nthe draft\n"
_DRAFT_DIGEST = compute_plan_draft_fingerprint(_DRAFT_TEXT)
_OTHER_DRAFT_TEXT = "# Plan\n\nsomebody else's draft\n"
_OTHER_DRAFT_DIGEST = compute_plan_draft_fingerprint(_OTHER_DRAFT_TEXT)


def _worktree_with_context(tmp_path: Path, session_id: str = "test1234") -> Path:
    worktree = tmp_path / "wt"
    claude_dir = worktree / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "cw-context.json").write_text(json.dumps({"session_id": session_id}))
    return worktree


def _write_draft(worktree: Path, text: str = _DRAFT_TEXT) -> Path:
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(parents=True, exist_ok=True)
    draft = cw_dir / "plan-draft.md"
    draft.write_text(text, encoding="utf-8")
    return draft


def _recorded_fingerprint(session_id: str = "test1234") -> object:
    sess = next(s for s in load_state().sessions if s.id == session_id)
    assert sess.last_result is not None
    return sess.last_result["plan_draft_fingerprint"]


def _seed_worker_session(
    tmp_path: Path, tmp_config_dir: Path, worktree: Path, **overrides: object
) -> None:
    """A DAEMON session whose recorded worktree is *worktree* -- the shape
    dispatch spawns, and the directory emit binds the draft against."""
    _seed_daemon_session(
        tmp_path,
        tmp_config_dir,
        session_id="test1234",
        worktree_path=worktree,
        **overrides,
    )


class TestResultEmitPlanDraftFingerprintBinding:
    """#2382: `cw result emit` never records the payload's fingerprint — a
    non-null value is only the producer's claim that a draft is in hand, and
    the digest is recomputed from the draft in the *target session's*
    worktree."""

    def test_truncated_claim_is_replaced_by_the_digest_cw_computes(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The incident shape: 62 of 64 characters. The recorded value is the
        on-disk digest, and stderr says the payload's value was replaced."""
        worktree = _worktree_with_context(tmp_path)
        _seed_worker_session(tmp_path, tmp_config_dir, worktree)
        _write_draft(worktree)
        monkeypatch.chdir(worktree)

        result = CliRunner().invoke(
            main, ["result", "emit", "-"], input=json.dumps(_claim(_DRAFT_DIGEST[:62]))
        )

        assert result.exit_code == 0, result.output
        assert _recorded_fingerprint() == _DRAFT_DIGEST
        assert "replaced the payload's value" in result.output

    def test_matching_claim_is_recorded_silently(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = _worktree_with_context(tmp_path)
        _seed_worker_session(tmp_path, tmp_config_dir, worktree)
        _write_draft(worktree)
        monkeypatch.chdir(worktree)

        result = CliRunner().invoke(
            main, ["result", "emit", "-"], input=json.dumps(_claim(_DRAFT_DIGEST))
        )

        assert result.exit_code == 0, result.output
        assert _recorded_fingerprint() == _DRAFT_DIGEST
        assert "replaced" not in result.output

    def test_null_claim_is_left_alone_even_when_a_draft_exists(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """null is the contract's "no draft" value on every non-plan-stage
        sentinel; a stale draft on disk must not turn it into a binding."""
        worktree = _worktree_with_context(tmp_path)
        _seed_worker_session(tmp_path, tmp_config_dir, worktree)
        _write_draft(worktree)
        monkeypatch.chdir(worktree)

        result = CliRunner().invoke(
            main, ["result", "emit", "-"], input=json.dumps(_claim(None))
        )

        assert result.exit_code == 0, result.output
        assert _recorded_fingerprint() is None

    def test_draft_is_resolved_against_the_target_sessions_worktree(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--session-id` from another directory must never bind the draft
        that happens to sit in the invoker's cwd to the target session."""
        session_wt = tmp_path / "session-wt"
        session_wt.mkdir()
        _seed_worker_session(tmp_path, tmp_config_dir, session_wt)
        _write_draft(session_wt, _DRAFT_TEXT)
        elsewhere = tmp_path / "operator-cwd"
        elsewhere.mkdir()
        _write_draft(elsewhere, _OTHER_DRAFT_TEXT)
        monkeypatch.chdir(elsewhere)

        result = CliRunner().invoke(
            main,
            ["result", "emit", "-", "--session-id", "test1234"],
            input=json.dumps(_claim("a" * 62)),
        )

        assert result.exit_code == 0, result.output
        assert _recorded_fingerprint() == _DRAFT_DIGEST
        assert _recorded_fingerprint() != _OTHER_DRAFT_DIGEST

    def test_session_without_worktree_falls_back_to_cwd(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        worktree = _worktree_with_context(tmp_path)
        _write_draft(worktree)
        monkeypatch.chdir(worktree)

        result = CliRunner().invoke(
            main, ["result", "emit", "-"], input=json.dumps(_claim("a" * 62))
        )

        assert result.exit_code == 0, result.output
        assert _recorded_fingerprint() == _DRAFT_DIGEST

    def test_claim_without_a_draft_exits_one_and_mutates_nothing(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = _worktree_with_context(tmp_path)
        _seed_worker_session(tmp_path, tmp_config_dir, worktree)
        monkeypatch.chdir(worktree)

        result = CliRunner().invoke(
            main, ["result", "emit", "-"], input=json.dumps(_claim("a" * 64))
        )

        assert result.exit_code == 1
        assert "claims a plan draft but none exists" in result.output
        assert str(worktree / ".cw" / "plan-draft.md") in result.output
        assert "No session state was modified." in result.output
        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is None

    def test_unreadable_draft_exits_one_and_mutates_nothing(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = _worktree_with_context(tmp_path)
        _seed_worker_session(tmp_path, tmp_config_dir, worktree)
        draft = _write_draft(worktree)
        draft.write_bytes(b"\xff\xfe not utf-8")
        monkeypatch.chdir(worktree)

        result = CliRunner().invoke(
            main, ["result", "emit", "-"], input=json.dumps(_claim("a" * 64))
        )

        assert result.exit_code == 1
        assert "cannot read" in result.output
        assert "No session state was modified." in result.output
        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is None

    def test_plan_draft_option_overrides_the_worktree_default(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        session_wt = tmp_path / "session-wt"
        session_wt.mkdir()
        _seed_worker_session(tmp_path, tmp_config_dir, session_wt)
        _write_draft(session_wt, _OTHER_DRAFT_TEXT)
        elsewhere = tmp_path / "elsewhere" / "draft.md"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text(_DRAFT_TEXT, encoding="utf-8")
        payload_file = tmp_path / "payload.json"
        payload_file.write_text(json.dumps(_claim("a" * 62)))

        result = CliRunner().invoke(
            main,
            [
                "result",
                "emit",
                str(payload_file),
                "--session-id",
                "test1234",
                "--plan-draft",
                str(elsewhere),
            ],
        )

        assert result.exit_code == 0, result.output
        assert _recorded_fingerprint() == _DRAFT_DIGEST

    def test_binding_runs_before_validation(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-string claim is still a claim: it is replaced by the on-disk
        digest rather than rejected by the schema's shape validator."""
        worktree = _worktree_with_context(tmp_path)
        _seed_worker_session(tmp_path, tmp_config_dir, worktree)
        _write_draft(worktree)
        monkeypatch.chdir(worktree)

        result = CliRunner().invoke(
            main, ["result", "emit", "-"], input=json.dumps(_claim(12345))
        )

        assert result.exit_code == 0, result.output
        assert _recorded_fingerprint() == _DRAFT_DIGEST

    def test_already_recorded_short_circuits_before_binding(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repeat emit against a session that already carries a terminal
        result has nothing to fix: it lands on 'already recorded' even when
        the claimed draft is gone (promoted away) and the claim is malformed,
        and it prints no 'replaced' note for a write that did not happen."""
        worktree = _worktree_with_context(tmp_path)
        _seed_worker_session(
            tmp_path,
            tmp_config_dir,
            worktree,
            last_result={"status": "plan_pending_approval"},
            last_result_source=LastResultSource.EMIT_CLI,
        )
        monkeypatch.chdir(worktree)

        result = CliRunner().invoke(
            main, ["result", "emit", "-"], input=json.dumps(_claim("a" * 62))
        )

        assert result.exit_code == 0, result.output
        assert "Result already recorded for session test1234" in result.output
        assert "source=emit_cli" in result.output
        assert "replaced" not in result.output
        assert "claims a plan draft" not in result.output
        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result == {"status": "plan_pending_approval"}


class TestReconstructStagedSentinelLegacyFingerprint:
    """#2382: the new shape validator must not make an already-persisted
    result unreconstructable on the Stop-hook / phantom-sweep path."""

    def test_malformed_persisted_fingerprint_reconstructs_with_null(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from cw.result import reconstruct_staged_sentinel

        persisted = _claim("c" * 62)
        with caplog.at_level(logging.WARNING, logger="cw.result"):
            reconstructed = reconstruct_staged_sentinel(persisted)

        assert isinstance(reconstructed, AutoDevResult)
        assert reconstructed.status == "plan_pending_approval"
        assert reconstructed.plan_draft_fingerprint is None
        assert "got 62 characters" in caplog.text
        assert "c" * 62 not in caplog.text
        # The stored dict is untouched -- sanitization works on a copy.
        assert persisted["plan_draft_fingerprint"] == "c" * 62

    def test_well_formed_persisted_fingerprint_is_kept(self) -> None:
        from cw.result import reconstruct_staged_sentinel

        reconstructed = reconstruct_staged_sentinel(_claim("d" * 64))

        assert isinstance(reconstructed, AutoDevResult)
        assert reconstructed.plan_draft_fingerprint == "d" * 64


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


# ---------------------------------------------------------------------------
# opencode result-door collision tests (#1671 R8)
# ---------------------------------------------------------------------------


class TestOpencodeDoorCollision:
    """opencode-specific collision scenarios for the result door.

    opencode writes through the door from two sources:
    - EXECUTOR_DIRECT: OpencodeExecutor's synchronous pre-flight failure path
    - GIT_SYNTHESIS: harvest path via synthesize_opencode_result

    These tests verify first-writer-wins holds for opencode's two sources
    racing each other and racing external writers (stop-hook, salvage).
    """

    def test_executor_direct_wins_over_harvest_synthesis(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """EXECUTOR_DIRECT (opencode spawn failure) writes first → harvest refused."""
        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="oc-coll-1",
            last_result={
                "status": "blocked",
                "blocker": {"reason": "opencode_not_found"},
            },
            last_result_source=LastResultSource.EXECUTOR_DIRECT,
        )
        with sessions_lock():
            outcome = emit_result_locked(
                _valid_payload(), "oc-coll-1", source=LastResultSource.GIT_SYNTHESIS
            )
        assert outcome.refused is True
        assert outcome.existing_source == LastResultSource.EXECUTOR_DIRECT

    def test_harvest_synthesis_wins_over_executor_direct(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """GIT_SYNTHESIS (opencode harvest) writes first → EXECUTOR_DIRECT refused."""
        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="oc-coll-2",
            last_result={"status": "shipped"},
            last_result_source=LastResultSource.GIT_SYNTHESIS,
        )
        with sessions_lock():
            outcome = emit_result_locked(
                _valid_payload(), "oc-coll-2", source=LastResultSource.EXECUTOR_DIRECT
            )
        assert outcome.refused is True
        assert outcome.existing_source == LastResultSource.GIT_SYNTHESIS

    def test_opencode_harvest_refusal_leaves_session_byte_identical(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Door refusal leaves session byte-identical (opencode harvest path)."""
        foreign = {"status": "blocked", "blocker": {"reason": "opencode_no_output"}}
        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="oc-coll-3",
            last_result=foreign,
            last_result_source=LastResultSource.EXECUTOR_DIRECT,
        )
        before = json.dumps(
            next(s for s in load_state().sessions if s.id == "oc-coll-3").model_dump(
                mode="json"
            ),
            sort_keys=True,
        )

        with sessions_lock():
            outcome = emit_result_locked(
                _valid_payload(),
                "oc-coll-3",
                source=LastResultSource.SALVAGE_TRANSCRIPT,
            )

        assert outcome.refused is True
        after = json.dumps(
            next(s for s in load_state().sessions if s.id == "oc-coll-3").model_dump(
                mode="json"
            ),
            sort_keys=True,
        )
        assert before == after
