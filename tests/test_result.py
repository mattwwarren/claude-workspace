"""Tests for cw.result — validate_payload helper and the emit command."""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from cw.auto_dev_result import AutoDevResult, BlockedResult
from cw.cli import main
from cw.config import load_state, save_state, sessions_lock
from cw.dev_queue import load_dev_queue
from cw.events import read_events
from cw.exceptions import EmitSessionNotFoundError, EmitValidationError
from cw.models import (
    STAGED_EMIT_RESULT_KEY,
    CwState,
    LastResultSource,
    OrchestratorEventType,
    Session,
    SessionPurpose,
    SessionStatus,
)
from cw.plan_fingerprint import compute_plan_draft_fingerprint
from cw.result import (
    EmitOutcome,
    _validate_or_exit,
    emit_result,
    emit_result_locked,
    emit_result_on,
    has_terminal_result,
    validate_payload,
)
from tests._clients_yaml import staged_client, write_clients_yaml
from tests.conftest import (
    _REPO_ROOT,
    _audit_failure_logged,
    _fail_audit_append,
    _plan_pending_payload,
    _seed_daemon_session,
)

_PAYLOAD_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


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

    def test_emit_result_locked_records_result_emitted_audit_event(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """An accepted emit records exactly one audit-only SESSION_RESULT_EMITTED
        event, before save_state, with the ticket derived from the session
        name (#2439)."""
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        with sessions_lock():
            emit_result_locked(
                _valid_payload(), "test1234", source=LastResultSource.EMIT_CLI
            )

        events = read_events(event_types=[OrchestratorEventType.SESSION_RESULT_EMITTED])
        assert len(events) == 1
        event = events[0]
        assert event.correlation_id == "GEN-42"
        payload = event.payload
        assert set(payload) == {
            "session_id",
            "ticket_id",
            "client",
            "lane",
            "stage",
            "last_result_source",
            "status",
            "payload_digest",
            "actor",
            "recorded_at",
        }
        assert payload["session_id"] == "test1234"
        assert payload["ticket_id"] == "GEN-42"
        assert payload["client"] == "test-client"
        assert payload["lane"] is None
        assert payload["stage"] is None
        assert payload["last_result_source"] == "emit_cli"
        assert payload["status"] == "shipped"
        assert _PAYLOAD_DIGEST_RE.match(payload["payload_digest"])
        assert payload["actor"] == getpass.getuser()
        assert isinstance(payload["recorded_at"], str)
        from datetime import datetime

        assert datetime.fromisoformat(payload["recorded_at"])

    def test_emit_result_locked_refusal_records_no_audit_event(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A refused (already-terminal) write records no audit event."""
        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="test1234",
            last_result={"status": "shipped"},
            last_result_source=LastResultSource.STOP_HOOK_HARVEST,
        )
        with sessions_lock():
            outcome = emit_result_locked(
                _valid_payload(), "test1234", source=LastResultSource.EMIT_CLI
            )

        assert outcome.refused is True
        events = read_events(event_types=[OrchestratorEventType.SESSION_RESULT_EMITTED])
        assert events == []

    def test_emit_result_locked_audit_append_failure_still_persists_result(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Fail open (#2465): an audit-inbox failure is logged, not raised, and
        the accepted result still reaches ``save_state``."""
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        attempts = _fail_audit_append(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="cw.result"), sessions_lock():
            outcome = emit_result_locked(
                _valid_payload(), "test1234", source=LastResultSource.EMIT_CLI
            )

        assert outcome.refused is False
        assert outcome.result is not None
        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is not None
        assert sess.last_result["status"] == "shipped"
        assert sess.last_result_source == LastResultSource.EMIT_CLI
        assert sess.status.value == "active"
        assert attempts == [OrchestratorEventType.SESSION_RESULT_EMITTED]
        digest = hashlib.sha256(
            json.dumps(outcome.result.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        assert _audit_failure_logged(
            caplog,
            session_id="test1234",
            source="emit_cli",
            status="shipped",
            payload_digest=digest,
        )

    def test_emit_result_locked_refusal_with_failing_audit_mutates_nothing(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A first-writer-wins refusal never reaches the audit append (#2465):
        no event, no mutation, and no audit-failure log even when the inbox is
        broken."""
        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="test1234",
            last_result={"status": "shipped"},
            last_result_source=LastResultSource.STOP_HOOK_HARVEST,
        )
        before = load_state().model_dump(mode="json")
        attempts = _fail_audit_append(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="cw.result"), sessions_lock():
            outcome = emit_result_locked(
                _valid_payload(), "test1234", source=LastResultSource.EMIT_CLI
            )

        assert outcome.refused is True
        assert attempts == []
        assert load_state().model_dump(mode="json") == before
        assert not _audit_failure_logged(caplog, session_id="test1234")

    def test_emit_result_locked_state_save_failure_not_swallowed_by_fail_open(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fail-open covers ONLY the audit append (#2465): with the audit inbox
        broken, a later ``save_state`` failure still surfaces as the
        state-write failure and nothing is persisted."""
        import cw.result as result_mod

        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        before = load_state().model_dump(mode="json")
        _fail_audit_append(monkeypatch)

        def _raise_save(*_args: object, **_kwargs: object) -> None:
            msg = "state file unwritable"
            raise OSError(msg)

        monkeypatch.setattr(result_mod, "save_state", _raise_save)

        with sessions_lock(), pytest.raises(OSError, match="state file unwritable"):
            emit_result_locked(
                _valid_payload(), "test1234", source=LastResultSource.EMIT_CLI
            )

        assert load_state().model_dump(mode="json") == before

    def test_emit_result_on_audited_audit_failure_still_mutates_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The in-memory seam the reconcile result-write paths use (#2465): a
        failing audit append is logged and the accepted mutation stands, for
        the caller's own trailing ``save_state`` to flush."""
        from cw.result import emit_result_on_audited

        session = _in_memory_session()
        _fail_audit_append(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="cw.result"):
            outcome = emit_result_on_audited(
                session, _valid_payload(), source=LastResultSource.GIT_SYNTHESIS
            )

        assert outcome.refused is False
        assert session.last_result is not None
        assert session.last_result["status"] == "shipped"
        assert session.last_result_source == LastResultSource.GIT_SYNTHESIS
        assert _audit_failure_logged(caplog, session_id="sess1234")

    def test_emit_result_locked_save_state_failure_raises_with_audit_event_recorded(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A save_state failure after the audit event has already landed still
        propagates, and the audit event stands as a truthful record (mirrors
        test_revoke_plan_approval_save_failure_raises_with_event_recorded)."""
        import cw.result as result_mod

        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")

        def _raise_save(*_args: object, **_kwargs: object) -> None:
            msg = "state file unwritable"
            raise OSError(msg)

        monkeypatch.setattr(result_mod, "save_state", _raise_save)

        with sessions_lock(), pytest.raises(OSError, match="state file unwritable"):
            emit_result_locked(
                _valid_payload(), "test1234", source=LastResultSource.EMIT_CLI
            )

        events = read_events(event_types=[OrchestratorEventType.SESSION_RESULT_EMITTED])
        assert len(events) == 1

    def test_emit_result_locked_audit_ticket_id_for_blocked_shape(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A BlockedResult-shaped payload (no schema_version) still records a
        populated ticket_id, derived from the session name rather than the
        result object (#2439)."""
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        payload = {
            "status": "blocked",
            "blocker": {"stage": "s2", "reason": "impl_failed", "details": "x"},
        }
        with sessions_lock():
            outcome = emit_result_locked(
                payload, "test1234", source=LastResultSource.STOP_HOOK_HARVEST
            )

        assert outcome.refused is False
        assert isinstance(outcome.result, BlockedResult)
        events = read_events(event_types=[OrchestratorEventType.SESSION_RESULT_EMITTED])
        assert len(events) == 1
        assert events[0].payload["ticket_id"] == "GEN-42"


def test_session_result_emitted_absent_from_reconcile_and_dispatch_consumer_sets() -> (
    None
):
    """SESSION_RESULT_EMITTED is audit-only by construction (#2439, R2): it
    must never appear in a reconcile/dispatch consumer's event_types filter,
    nor be referenced at all under those trees. Mirrors
    tests/test_result_door_guard.py's regex + Path.rglob("*.py") shape."""
    consumer_source = (_REPO_ROOT / "src" / "cw" / "dispatch" / "loop.py").read_text()
    match = re.search(
        r"consume_completed_sessions.*?event_types=\[(.*?)\]",
        consumer_source,
        re.DOTALL,
    )
    assert match is not None, "consume_completed_sessions event_types filter not found"
    assert "SESSION_RESULT_EMITTED" not in match.group(1)

    for tree in ("reconcile", "dispatch"):
        root = _REPO_ROOT / "src" / "cw" / tree
        for path in root.rglob("*.py"):
            text = path.read_text()
            assert "SESSION_RESULT_EMITTED" not in text, (
                f"{path} references SESSION_RESULT_EMITTED; this event is "
                "audit-only and must never be consumed by reconcile/dispatch"
            )


def test_session_result_emitted_is_documented() -> None:
    """docs/events.md documents session.result_emitted (mirrors
    tests/test_dispatch_usage_limit.py's test_event_type_is_documented)."""
    events_doc = (_REPO_ROOT / "docs" / "events.md").read_text()
    assert "### `session.result_emitted`" in events_doc


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

    def test_audit_append_failure_still_records_result(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Direct ``cw result emit`` fails open (#2465): a broken audit inbox
        is logged, the result persists, and the command exits 0 as usual."""
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        _fail_audit_append(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="cw.result"):
            result = CliRunner().invoke(
                main,
                ["result", "emit", "-", "--session-id", "test1234"],
                input=json.dumps(_valid_payload()),
            )

        assert result.exit_code == 0, result.output
        assert "Recorded result for session test1234: status=shipped" in result.output
        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is not None
        assert sess.last_result["status"] == "shipped"
        assert sess.last_result_source == LastResultSource.EMIT_CLI
        assert _audit_failure_logged(caplog, session_id="test1234")

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

    def test_validate_or_exit_rejects_bare_blocked_result_shape(self) -> None:
        """#2458: pins the CLI gate contract ``_validate_or_exit`` documents --
        ``cw result emit`` stages ``AutoDevResult`` only, never a bare
        ``BlockedResult``. The idle sweep now handles ``landed_terminal``
        (#2482) defensively if the gate is widened. The payload is a genuine
        ``BlockedResult``, so a failure here means the gate itself was
        widened (as #1457 widened the harvest door), not that the fixture
        drifted."""
        blocked_payload = {
            "status": "blocked",
            "blocker": {"stage": "s1", "reason": "validation_failed", "details": "x"},
        }
        assert isinstance(BlockedResult.model_validate(blocked_payload), BlockedResult)

        with pytest.raises(click.exceptions.Exit) as exc_info:
            _validate_or_exit(blocked_payload)

        assert exc_info.value.exit_code == 1

    def test_result_module_does_not_import_cw_cli(self) -> None:
        """#2458: ``cw.result`` sits below ``cw.cli`` (``cw.cli`` imports it);
        its cw-context.json read and stamp write go through
        ``cw._hook_context``, never ``cw.cli._hook_io``."""
        import cw.result as result_mod

        source = Path(result_mod.__file__).read_text(encoding="utf-8")
        assert "from cw.cli" not in source
        assert "import cw.cli" not in source

    # ------------------------------------------------------------------
    # #2458 round 2: ``_stamp_staged_emit_result``'s write side had zero
    # coverage -- every case above uses ``_seed_daemon_session``, which never
    # writes a ``cw-context.json`` at the test process's cwd, so the stamp's
    # own first-line no-op (``context is None``) always short-circuited it
    # before either the ``session_id`` match check or the locked write ran.
    # ------------------------------------------------------------------

    def test_emit_stamps_staged_emit_result_flag_when_context_names_this_session(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        worktree = _worktree_with_context(tmp_path, session_id="test1234")
        monkeypatch.chdir(worktree)

        result = CliRunner().invoke(
            main, ["result", "emit", "-"], input=json.dumps(_valid_payload())
        )

        assert result.exit_code == 0, result.output
        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert context[STAGED_EMIT_RESULT_KEY] is True

    def test_emit_does_not_stamp_when_context_names_a_different_session(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The foreign-worktree guard the docstring describes: ``--session-id``
        overrides which session is recorded, but the stamp must not touch a
        cw-context.json naming an unrelated session."""
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        worktree = _worktree_with_context(tmp_path, session_id="other999")
        monkeypatch.chdir(worktree)

        result = CliRunner().invoke(
            main,
            ["result", "emit", "-", "--session-id", "test1234"],
            input=json.dumps(_valid_payload()),
        )

        assert result.exit_code == 0, result.output
        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is not None
        context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
        assert STAGED_EMIT_RESULT_KEY not in context

    def test_emit_does_not_stamp_when_cwd_has_no_context_file(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_daemon_session(tmp_path, tmp_config_dir, session_id="test1234")
        no_context = tmp_path / "no-context"
        no_context.mkdir()
        monkeypatch.chdir(no_context)

        result = CliRunner().invoke(
            main,
            ["result", "emit", "-", "--session-id", "test1234"],
            input=json.dumps(_valid_payload()),
        )

        assert result.exit_code == 0, result.output
        sess = next(s for s in load_state().sessions if s.id == "test1234")
        assert sess.last_result is not None
        assert not (no_context / ".claude" / "cw-context.json").exists()

    def test_emit_then_stop_hook_routes_the_staged_result(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Write and read halves together: ``cw result emit`` stamps the
        flag, then a real Stop-hook fire with ``background_tasks`` pending
        reads it back and routes on it (#2458's lock-free peek), instead of
        every Stop-hook test continuing to hand-stamp the flag directly."""
        from cw.dev_queue import save_dev_queue
        from cw.models import DevQueueStore, QueueItemStatus, Stage, TicketTask
        from cw.native_daemon import FakeNativeDaemonClient

        ticket_id = "GEN-2458-emit-stop"
        write_clients_yaml(staged_client("test-client", "/tmp/ws-test"))
        worktree = _worktree_with_context(tmp_path, session_id="test1234")
        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="test1234",
            worktree_path=worktree,
            surface_ref="sfref-2458-emit-stop",
        )
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id=ticket_id,
                        client="test-client",
                        status=QueueItemStatus.RUNNING,
                        session_id="test1234",
                        attempts=1,
                        stage=Stage.FINALIZE,
                    )
                ]
            )
        )
        (worktree / ".claude" / "cw-context.json").write_text(
            json.dumps(
                {"session_id": "test1234", "headless": True, "ticket_id": ticket_id}
            )
        )
        fake_home = tmp_path / "fake-home-2458-emit-stop"
        fake_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("cw.cli.sessions.Path.home", lambda: fake_home)
        daemon = FakeNativeDaemonClient()
        monkeypatch.setattr(
            "cw.cli.stop_hook.command.get_native_daemon_client", lambda: daemon
        )

        monkeypatch.chdir(worktree)
        emit_result_ = CliRunner().invoke(
            main,
            ["result", "emit", "-"],
            input=json.dumps({**_valid_payload(), "ticket_id": ticket_id}),
        )
        assert emit_result_.exit_code == 0, emit_result_.output

        stop_body = {
            "session_id": "sfref-2458-emit-stop-uuid",
            "cwd": str(worktree),
            "hook_event_name": "Stop",
            "background_tasks": [{"id": "bg-1", "description": "still running"}],
        }
        stop_result = CliRunner().invoke(
            main, ["signal-stop"], input=json.dumps(stop_body)
        )
        assert stop_result.exit_code == 0, stop_result.output

        task = next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)
        assert task.status == QueueItemStatus.COMPLETED
        updated = next(s for s in load_state().sessions if s.id == "test1234")
        assert updated.status == SessionStatus.ACTIVE  # session stays live for bg work


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

    def test_omitted_fingerprint_key_is_treated_like_null(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """opencode's _PLAN_SENTINEL_TEMPLATE has no plan_draft_fingerprint key
        at all -- a missing key must resolve identically to an explicit null,
        never bind a stale on-disk draft that happens to exist (#2430)."""
        worktree = _worktree_with_context(tmp_path)
        _seed_worker_session(tmp_path, tmp_config_dir, worktree)
        _write_draft(worktree)
        monkeypatch.chdir(worktree)
        payload = _plan_pending_payload(
            schema_version=8, ticket_id="GEN-2382", plan_draft_fingerprint="a" * 64
        )
        del payload["plan_draft_fingerprint"]

        result = CliRunner().invoke(
            main, ["result", "emit", "-"], input=json.dumps(payload)
        )

        assert result.exit_code == 0, result.output
        assert _recorded_fingerprint() is None


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

    def test_validation_failure_logs_warning_with_errors(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#2458: a staged result that fails validation says why, then
        returns None so every caller still falls back rather than raising."""
        from cw.result import reconstruct_staged_sentinel

        with caplog.at_level(logging.WARNING, logger="cw.result"):
            reconstructed = reconstruct_staged_sentinel({"status": "shipped"})

        assert reconstructed is None
        messages = [r.getMessage() for r in caplog.records if r.name == "cw.result"]
        assert len(messages) == 1
        assert "reconstruct_staged_sentinel: validation failed" in messages[0]
        # The pydantic field-error text, not just a generic failure line.
        assert "schema_version: Field required" in messages[0]


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

    def test_emit_cli_wins_over_executor_direct(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """EMIT_CLI (opencode's --session-id push) writes first →
        EXECUTOR_DIRECT refused."""
        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="oc-coll-4",
            last_result={"status": "shipped"},
            last_result_source=LastResultSource.EMIT_CLI,
        )
        with sessions_lock():
            outcome = emit_result_locked(
                _valid_payload(), "oc-coll-4", source=LastResultSource.EXECUTOR_DIRECT
            )
        assert outcome.refused is True
        assert outcome.existing_source == LastResultSource.EMIT_CLI

    def test_executor_direct_wins_over_emit_cli(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """EXECUTOR_DIRECT (opencode pre-flight failure) writes first →
        EMIT_CLI refused."""
        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="oc-coll-5",
            last_result={
                "status": "blocked",
                "blocker": {"reason": "opencode_not_found"},
            },
            last_result_source=LastResultSource.EXECUTOR_DIRECT,
        )
        with sessions_lock():
            outcome = emit_result_locked(
                _valid_payload(), "oc-coll-5", source=LastResultSource.EMIT_CLI
            )
        assert outcome.refused is True
        assert outcome.existing_source == LastResultSource.EXECUTOR_DIRECT

    def test_emit_cli_wins_over_git_synthesis(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """EMIT_CLI writes first → GIT_SYNTHESIS (opencode harvest fallback) refused.

        The pairing that matters most for the adopted table shape: once a
        worker's session id is known, its emit_cli push is primary and the
        git-facts harvest is the fallback, mirroring the Claude daemon.
        """
        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="oc-coll-6",
            last_result={"status": "shipped"},
            last_result_source=LastResultSource.EMIT_CLI,
        )
        with sessions_lock():
            outcome = emit_result_locked(
                _valid_payload(), "oc-coll-6", source=LastResultSource.GIT_SYNTHESIS
            )
        assert outcome.refused is True
        assert outcome.existing_source == LastResultSource.EMIT_CLI

    def test_git_synthesis_wins_over_emit_cli(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """GIT_SYNTHESIS writes first (emit never ran) → EMIT_CLI refused."""
        _seed_daemon_session(
            tmp_path,
            tmp_config_dir,
            session_id="oc-coll-7",
            last_result={"status": "shipped"},
            last_result_source=LastResultSource.GIT_SYNTHESIS,
        )
        with sessions_lock():
            outcome = emit_result_locked(
                _valid_payload(), "oc-coll-7", source=LastResultSource.EMIT_CLI
            )
        assert outcome.refused is True
        assert outcome.existing_source == LastResultSource.GIT_SYNTHESIS
