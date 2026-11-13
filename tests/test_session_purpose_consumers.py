"""Consumers of the new session purposes PLAN, REVIEW, FINALIZE (#3054).

The enum grew three values for codex stage sessions. These pin the consumers a
REVIEW session reaches: persistence, the ``impl`` lookup that must no longer
match it, the purpose prompt, and the client-config purpose validation.

#3253 adds the consumers the #3054 plan listed that had no test yet: the
``cw start --purpose`` rejection, ``start_session`` not resuming a REVIEW
session, the operator-close audit purpose, the ``DEFAULT_AUTO_PURPOSES`` pin,
the ``cw list`` purpose column, ``summarise_session``, and the
ORCHESTRATE-only exclusions in ``compute_drift``, ``_is_unobservable_session``,
``_is_null_liveness_candidate`` and ``routed_marker_rejection``. The consumers
whose builders live in another test module are tested there instead
(``test_session.py``, ``test_doctor.py``, ``test_board.py``,
``test_session_inspect.py``, ``test_orchestrate.py``, ``test_codex_executor.py``,
``test_executor_core.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.config import _validate_purposes, load_state, save_state
from cw.doctor.wedge.orphans import _is_null_liveness_candidate
from cw.exceptions import CwError
from cw.history import HistoryEvent
from cw.models import (
    DEFAULT_AUTO_PURPOSES,
    CwState,
    Session,
    SessionPurpose,
    SessionStatus,
)
from cw.orchestrate import summarise_session
from cw.prompts import get_purpose_prompt
from cw.reconcile._shared._roster import compute_drift
from cw.reconcile.liveness import _is_unobservable_session
from cw.reconcile.routed_result_sessions import (
    MarkerRejection,
    routed_marker_rejection,
)
from cw.reconcile.session_close import record_operator_close
from cw.session import start_session
from tests._clients_yaml import write_clients_yaml
from tests._shared.session_seeding import _make_daemon_session, _seed_sessions

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.native_daemon import FakeNativeDaemonClient

NEW_PURPOSES = (SessionPurpose.PLAN, SessionPurpose.REVIEW, SessionPurpose.FINALIZE)


def _noop(*_args: object, **_kwargs: object) -> None:
    pass


@pytest.mark.parametrize("purpose", NEW_PURPOSES)
def test_persisted_session_round_trips_new_purpose(
    tmp_config_dir: Path, purpose: SessionPurpose
) -> None:
    save_state(CwState(sessions=[_make_daemon_session(id="rt", purpose=purpose)]))
    loaded = load_state().sessions[0]
    assert loaded.purpose is purpose


def test_impl_lookup_no_longer_matches_codex_review_session(
    tmp_config_dir: Path,
) -> None:
    save_state(
        CwState(
            sessions=[
                _make_daemon_session(
                    id="codex-rev", client="test", purpose=SessionPurpose.REVIEW
                )
            ]
        )
    )
    state = load_state()
    assert state.find_session("test", "impl") is None
    found = state.find_session("test", "review")
    assert found is not None
    assert found.id == "codex-rev"


def test_purpose_prompt_is_none_for_review_without_raising() -> None:
    assert get_purpose_prompt("review") is None


def test_auto_purposes_accepts_new_values() -> None:
    _validate_purposes(["review", "plan", "finalize"])


def test_auto_purposes_still_rejects_unknown_value() -> None:
    with pytest.raises(CwError, match="Invalid purpose 'bogus'"):
        _validate_purposes(["bogus"])


# ---- #3253: the remaining #3054 consumers -------------------------------------


@pytest.mark.parametrize("purpose", NEW_PURPOSES)
def test_cw_start_rejects_new_purposes(purpose: SessionPurpose) -> None:
    """Item 3: codex stage purposes are never a ``cw start --purpose`` choice."""
    result = CliRunner().invoke(main, ["start", "--purpose", purpose.value, "c"])
    assert result.exit_code == 2


def test_start_session_does_not_resume_a_review_session(
    tmp_config_dir: Path,
    sample_client: ClientConfig,
    mock_native_daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Item 5: an ACTIVE REVIEW session is not the client's impl session."""
    write_clients_yaml(sample_client)
    monkeypatch.setattr("cw.session._write_hook_context", _noop)
    monkeypatch.setattr("cw.session._attach_session", _noop)
    review = Session(
        id="review01",
        name="test-client/review",
        client="test-client",
        purpose=SessionPurpose.REVIEW,
        status=SessionStatus.ACTIVE,
        workspace_path=sample_client.workspace_path,
    )
    save_state(CwState(sessions=[review]))

    start_session("test-client", "impl", native_daemon=mock_native_daemon)

    assert "already active" not in capsys.readouterr().out.lower()
    assert len(mock_native_daemon.spawn_calls) == 1
    purposes = {s.purpose for s in load_state().sessions if s.client == "test-client"}
    assert purposes == {SessionPurpose.REVIEW, SessionPurpose.IMPL}


def test_operator_close_audit_records_the_review_purpose() -> None:
    """Item 9: the history audit event carries the closed session's purpose."""
    captured: list[HistoryEvent] = []

    def _capture(_client: str, event: HistoryEvent) -> None:
        captured.append(event)

    record_operator_close(
        _make_daemon_session(purpose=SessionPurpose.REVIEW),
        command="cw spawn close",
        prior_status=SessionStatus.ACTIVE,
        ticket_id="T-1",
        record=_capture,
    )
    assert [event.purpose for event in captured] == [SessionPurpose.REVIEW]


def test_default_auto_purposes_exclude_new_purposes() -> None:
    """Item 13: auto-started purposes stay IDEA, IMPL, DEBT."""
    assert DEFAULT_AUTO_PURPOSES == [
        SessionPurpose.IDEA,
        SessionPurpose.IMPL,
        SessionPurpose.DEBT,
    ]
    assert not set(NEW_PURPOSES) & set(DEFAULT_AUTO_PURPOSES)


def test_cw_list_prints_finalize_in_the_purpose_column(tmp_config_dir: Path) -> None:
    """Item 16: the 8-character ``finalize`` fits the 10-wide purpose column."""
    _seed_sessions(
        _make_daemon_session(
            id="fin1", purpose=SessionPurpose.FINALIZE, status=SessionStatus.ACTIVE
        )
    )
    result = CliRunner().invoke(main, ["list"])
    assert result.exit_code == 0, result.output
    assert f"{'finalize':<10} active" in result.output


@pytest.mark.parametrize("purpose", NEW_PURPOSES)
def test_summarise_session_reports_new_purpose(purpose: SessionPurpose) -> None:
    """Item 17: the orchestrate summary carries the purpose value."""
    assert summarise_session(_make_daemon_session(purpose=purpose)).purpose == (
        purpose.value
    )


_COUNTED = (
    SessionPurpose.IMPL,
    SessionPurpose.PLAN,
    SessionPurpose.REVIEW,
    SessionPurpose.FINALIZE,
)
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
_LATER = _EPOCH + timedelta(days=30)


def _unobservable(purpose: SessionPurpose) -> Session:
    return _make_daemon_session(
        purpose=purpose, surface_ref=None, claude_session_id=None, local_liveness=None
    )


@pytest.mark.parametrize("purpose", _COUNTED)
def test_compute_drift_counts_non_orchestrate_phantoms(
    purpose: SessionPurpose,
) -> None:
    """Item 18: only ORCHESTRATE is skipped by the phantom sweep."""
    session = _make_daemon_session(
        id="ph1", purpose=purpose, surface_ref="gone", started_at=_EPOCH
    )
    report = compute_drift(CwState(sessions=[session]), set(), now=_LATER)
    assert report.phantom_session_ids == ["ph1"]


def test_compute_drift_skips_orchestrate() -> None:
    session = _make_daemon_session(
        id="orc1",
        purpose=SessionPurpose.ORCHESTRATE,
        surface_ref="gone",
        started_at=_EPOCH,
    )
    report = compute_drift(CwState(sessions=[session]), set(), now=_LATER)
    assert report.phantom_session_ids == []


@pytest.mark.parametrize(
    ("purpose", "expected"),
    [*((p, True) for p in _COUNTED), (SessionPurpose.ORCHESTRATE, False)],
)
def test_unobservable_session_excludes_only_orchestrate(
    purpose: SessionPurpose, expected: bool
) -> None:
    """Item 18: the #2417 unobservable population."""
    assert _is_unobservable_session(_unobservable(purpose)) is expected


@pytest.mark.parametrize(
    ("purpose", "expected"),
    [*((p, True) for p in _COUNTED), (SessionPurpose.ORCHESTRATE, False)],
)
def test_null_liveness_candidate_excludes_only_orchestrate(
    purpose: SessionPurpose, expected: bool
) -> None:
    """Item 18: doctor wedge class 9 candidates."""
    session = _make_daemon_session(
        purpose=purpose, surface_ref=None, local_liveness=None, started_at=_EPOCH
    )
    assert _is_null_liveness_candidate(session, _LATER) is expected


@pytest.mark.parametrize("purpose", NEW_PURPOSES)
def test_routed_marker_rejection_treats_new_purposes_like_impl(
    purpose: SessionPurpose,
) -> None:
    """Item 18: the routed-marker filter rejects only ORCHESTRATE as orchestrator."""
    impl_twin = routed_marker_rejection(_make_daemon_session())
    assert routed_marker_rejection(_make_daemon_session(purpose=purpose)) == impl_twin
    assert impl_twin is not MarkerRejection.ORCHESTRATOR


def test_routed_marker_rejection_flags_orchestrate() -> None:
    session = _make_daemon_session(purpose=SessionPurpose.ORCHESTRATE)
    assert routed_marker_rejection(session) is MarkerRejection.ORCHESTRATOR
