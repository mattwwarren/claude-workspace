"""Scoped ``cw doctor --reap --ticket <T> -c <C>`` (#2716).

Covers the scoped reap end to end: the ``client=`` keyword on the two
blocked-row helpers, the ``collapse_client=`` keyword on
``_reap_session_by_selector``, the scope enforcement inside
``_reap_wedge_findings`` (RUNNING loop, helper calls, session-record client
check for class 6/8, routed/leaked/outbox gates) and the scoped
``run_doctor`` path, which skips the global reconcile and preflight battery.

Every cross-client case seeds the SAME ticket number under ``acme`` and
``other``: ticket numbers repeat across clients, which is the leak the scope
closes. Tests marked "#2928" characterize the CURRENT unscoped
cross-client behavior and change when #2928 makes the unscoped reap
client-precise.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

import cw.doctor
from cw.config import load_state, state_file
from cw.dev_queue import load_dev_queue
from cw.doctor import ReapScope, format_report_json, run_doctor
from cw.doctor._shared import REAP_COMMAND, CheckResult, WedgeFinding
from cw.doctor.core import (
    _collect_scoped_wedge_findings,
    _collect_wedge_findings,
    _scope_finding_recipe,
)
from cw.doctor.loop_health import _reap_session_by_selector
from cw.doctor.pending_skip_wedge import (
    PENDING_SKIP_WEDGE_THRESHOLD,
    WEDGE_PENDING_WORKTREE_OCCUPIED,
    PendingWorktreeOccupiedFinding,
)
from cw.doctor.routed_result_wedge import WEDGE_ROUTED_RESULT_STRANDED
from cw.doctor.wedge import (
    _check_wedge_active_daemon_stale_no_sentinel,
    _check_wedge_dead_session_blocked_on_user,
)
from cw.doctor.wedge._constants import (
    _WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL,
    _WEDGE_ACTIVE_NO_DAEMON_ENTRY,
    _WEDGE_BLOCKED_DEAD_SESSION,
    _WEDGE_LEAKED_DAEMON_WORKER,
    _WEDGE_TERMINAL_SIBLING,
)
from cw.doctor.wedge.blocked_on_user import (
    _cancel_terminal_sibling_parks,
    _collapse_blocked_on_user_tasks,
)
from cw.doctor.wedge.reap import (
    _note_skipped_sessions,
    _reap_daemon_sessions,
    _reap_sessions_and_sweep,
    _reap_timeout_check,
    _reap_wedge_findings,
)
from cw.exceptions import SessionsLockTimeoutError
from cw.models import DevQueueStore, QueueItemStatus, ReapReason, SessionStatus
from tests._dispatch_tick_helpers import skip_tick_events
from tests._doctor_helpers import stub_daemon_roster
from tests._reconcile_helpers import _stamp_transcript_age
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    make_blocked,
    make_task,
    seed_queue,
)
from tests.conftest import _make_daemon_session

if TYPE_CHECKING:
    from cw.models import Session, TicketTask

OTHER = "other"
SCOPE = ReapScope(ACME, ACME_TICKET)
_TASK_RUNNING_NO_SESSION = "wedge/task-running-no-session"
_TASK_RUNNING_COMPLETED = "wedge/task-running-completed-session"
_GLOBAL_RECIPE_TAIL = "Run: cw doctor --reap to"
_SKIP_CHECK = "scoped-reap/session-skipped"
_SCOPE_CHECK = "scoped-reap/scope"
_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_STALE_MINUTES = 50  # past the STALE_45M liveness bucket
_ACME_SID = "sess-acme"
_OTHER_SID = "sess-other"
# One finding per condition `_seed_two_client_wedges` seeds under acme.
_SEEDED_ACME_CLASSES = (
    _TASK_RUNNING_NO_SESSION,
    _WEDGE_BLOCKED_DEAD_SESSION,
    _WEDGE_TERMINAL_SIBLING,
    _WEDGE_ACTIVE_NO_DAEMON_ENTRY,
)

_RAN_CHECKS = (
    "scoped-reap/running-task-revert",
    "scoped-reap/blocked-on-user-collapse",
    "scoped-reap/terminal-sibling-cancel",
    "scoped-reap/phantom-session-reap",
    "scoped-reap/daemon-stale-session-reap",
)
_SESSION_REAP_CHECKS = _RAN_CHECKS[3:]
_QUEUE_SIDE_CHECKS = _RAN_CHECKS[:3]
_INCOMPLETE_PREFIX = "attempted (incomplete, see wedge-reap):"
_NO_MATCH_PREFIX = "skipped (no matching finding):"
_SKIPPED_CHECKS = (
    "scoped-reap/leaked-worker-sweep",
    "scoped-reap/routed-result-close",
    "scoped-reap/routed-result-audit-outbox",
    "scoped-reap/global-reconcile",
    "scoped-reap/global-preflight-checks",
    "scoped-reap/advisory-detectors",
)

Calls = list[tuple[str, dict[str, object]]]


def _at(minutes: int) -> datetime:
    return _T0 + timedelta(minutes=minutes)


def _finding(
    wedge_class: str,
    *,
    ticket_id: str | None = ACME_TICKET,
    session_id: str | None = None,
    daemon_short_id: str | None = None,
) -> WedgeFinding:
    return WedgeFinding(
        wedge_class=wedge_class,
        session_id=session_id,
        ticket_id=ticket_id,
        recipe="recipe",
        state_file="state",
        daemon_short_id=daemon_short_id,
    )


def _blocked(client: str, minutes: int) -> TicketTask:
    """A dead-session BLOCKED_ON_USER ``ACM-1`` row under *client*."""
    return make_blocked(client=client, session_id=None, created_at=_at(minutes))


def _sibling(client: str, minutes: int) -> TicketTask:
    """A reap-eligible terminal_sibling ``ACM-1`` park under *client*."""
    return make_blocked(
        client=client,
        session_id=None,
        attempts=0,
        disposition=ReapReason.TERMINAL_SIBLING.value,
        created_at=_at(minutes),
    )


def _running(client: str, session_id: str | None = None) -> TicketTask:
    return make_task(
        client=client,
        status=QueueItemStatus.RUNNING,
        session_id=session_id,
        created_at=_at(0),
    )


def _session(
    client: str,
    session_id: str,
    ticket_id: str = ACME_TICKET,
    status: SessionStatus = SessionStatus.ACTIVE,
) -> Session:
    """A daemon session ``<client>/auto-dev/<ticket_id>`` (ACTIVE by default)."""
    return _make_daemon_session(
        id=session_id,
        client=client,
        name=f"{client}/auto-dev/{ticket_id}",
        surface_ref=f"ref-{session_id}",
        status=status,
    )


def _statuses(tasks: list[TicketTask]) -> list[tuple[str, QueueItemStatus]]:
    return [(t.client, t.status) for t in tasks]


def _disk_statuses() -> list[tuple[str, QueueItemStatus]]:
    return _statuses(load_dev_queue().tasks)


def _session_status(session_id: str) -> SessionStatus:
    return next(s.status for s in load_state().sessions if s.id == session_id)


def _record_selector(
    monkeypatch: pytest.MonkeyPatch, *, raise_on: set[str] | None = None
) -> Calls:
    """Patch ``reap``'s ``_reap_session_by_selector`` with a recorder."""
    calls: Calls = []

    def _fake(selector: str, **kwargs: object) -> bool:
        calls.append((selector, kwargs))
        if raise_on and selector in raise_on:
            raise _lock_timeout(state_file().parent)
        return True

    monkeypatch.setattr("cw.doctor.wedge.reap._reap_session_by_selector", _fake)
    return calls


def _lock_timeout(lock_dir: Path) -> SessionsLockTimeoutError:
    msg = "sessions lock held"
    return SessionsLockTimeoutError(
        msg, lock_path=lock_dir / ".sessions.lock", waited_s=60.0
    )


def _record_saves(monkeypatch: pytest.MonkeyPatch) -> list[DevQueueStore]:
    saves: list[DevQueueStore] = []
    monkeypatch.setattr("cw.doctor.wedge.reap.save_dev_queue", saves.append)
    return saves


def _raise_if_called(monkeypatch: pytest.MonkeyPatch, *targets: str) -> None:
    for target in targets:

        def _boom(*_a: object, _target: str = target, **_k: object) -> object:
            msg = f"{_target} must not run on the scoped path"
            raise AssertionError(msg)

        monkeypatch.setattr(target, _boom)


class TestHelperClientKeyword:
    """``client=`` on the two blocked-row helpers (R1)."""

    @staticmethod
    def _blocked_queue() -> DevQueueStore:
        # The other client's row is the oldest, so the default (ticket-id-only)
        # collapse and the scoped one pick different rows to keep.
        return DevQueueStore(
            tasks=[_blocked(OTHER, 0), _blocked(ACME, 1), _blocked(ACME, 2)]
        )

    @staticmethod
    def _sibling_queue() -> DevQueueStore:
        return DevQueueStore(tasks=[_sibling(ACME, 0), _sibling(OTHER, 1)])

    def test_collapse_with_client_leaves_other_client_untouched(self) -> None:
        queue = self._blocked_queue()

        assert _collapse_blocked_on_user_tasks(queue, {ACME_TICKET}, client=ACME)

        assert _statuses(queue.tasks) == [
            (OTHER, QueueItemStatus.BLOCKED_ON_USER),
            (ACME, QueueItemStatus.PENDING),
            (ACME, QueueItemStatus.CANCELLED),
        ]

    def test_cancel_with_client_leaves_other_client_untouched(self) -> None:
        queue = self._sibling_queue()

        assert _cancel_terminal_sibling_parks(queue, {ACME_TICKET}, client=ACME)

        assert _statuses(queue.tasks) == [
            (ACME, QueueItemStatus.CANCELLED),
            (OTHER, QueueItemStatus.BLOCKED_ON_USER),
        ]

    def test_default_client_keeps_ticket_id_only_matching(self) -> None:
        """Characterizes CURRENT behavior: ticket-id-only across clients (#2928)."""
        blocked = self._blocked_queue()
        siblings = self._sibling_queue()

        _collapse_blocked_on_user_tasks(blocked, {ACME_TICKET})
        _cancel_terminal_sibling_parks(siblings, {ACME_TICKET})

        assert _statuses(blocked.tasks) == [
            (OTHER, QueueItemStatus.PENDING),
            (ACME, QueueItemStatus.CANCELLED),
            (ACME, QueueItemStatus.CANCELLED),
        ]
        assert _statuses(siblings.tasks) == [
            (ACME, QueueItemStatus.CANCELLED),
            (OTHER, QueueItemStatus.CANCELLED),
        ]


class TestUnscopedCharacterization:
    """Characterizes CURRENT behavior: ticket-id-only grouping across clients.

    Client-precise unscoped reap is #2928; update these tests there.
    """

    @pytest.mark.parametrize("older", [ACME, OTHER])
    def test_class5_collapse_groups_both_clients(
        self, tmp_config_dir: Path, older: str
    ) -> None:
        newer = OTHER if older == ACME else ACME
        seed_queue([_blocked(newer, 5), _blocked(older, 0)])

        _reap_wedge_findings([_finding(_WEDGE_BLOCKED_DEAD_SESSION)])

        by_client = {t.client: t for t in load_dev_queue().tasks}
        assert by_client[older].status == QueueItemStatus.PENDING
        assert by_client[older].session_id is None
        assert by_client[newer].status == QueueItemStatus.CANCELLED

    def test_unscoped_running_rows_revert_for_both_clients(
        self, tmp_config_dir: Path
    ) -> None:
        seed_queue([_running(ACME, "s-a"), _running(OTHER, "s-o")])

        _reap_wedge_findings([_finding(_TASK_RUNNING_NO_SESSION)])

        assert _disk_statuses() == [
            (ACME, QueueItemStatus.PENDING),
            (OTHER, QueueItemStatus.PENDING),
        ]


class TestScopedQueuePass:
    def test_scoped_running_revert_touches_only_the_named_client(
        self, tmp_config_dir: Path
    ) -> None:
        seed_queue([_running(ACME, "s-a"), _running(OTHER, "s-o")])

        result = _reap_wedge_findings([_finding(_TASK_RUNNING_NO_SESSION)], scope=SCOPE)

        assert result is None
        acme_row, other_row = load_dev_queue().tasks
        assert acme_row.status == QueueItemStatus.PENDING
        assert acme_row.session_id is None
        assert other_row.status == QueueItemStatus.RUNNING
        assert other_row.session_id == "s-o"

    def test_scoped_class5_and_class7_pass_the_client(
        self, tmp_config_dir: Path
    ) -> None:
        seed_queue(
            [
                _blocked(OTHER, 0),
                _blocked(ACME, 1),
                _sibling(OTHER, 2),
                _sibling(ACME, 3),
            ]
        )

        _reap_wedge_findings(
            [
                _finding(_WEDGE_BLOCKED_DEAD_SESSION),
                _finding(_WEDGE_TERMINAL_SIBLING),
            ],
            scope=SCOPE,
        )

        assert _disk_statuses() == [
            (OTHER, QueueItemStatus.BLOCKED_ON_USER),
            (ACME, QueueItemStatus.PENDING),
            (OTHER, QueueItemStatus.BLOCKED_ON_USER),
            (ACME, QueueItemStatus.CANCELLED),
        ]


class TestReapLevelFencing:
    """Global-looking findings never reach a global mutation when scoped."""

    @staticmethod
    def _wire(monkeypatch: pytest.MonkeyPatch) -> dict[str, Calls]:
        calls: dict[str, Calls] = {"pending": [], "routed": [], "sweep": []}

        def _pending() -> bool:
            calls["pending"].append(("", {}))
            return True

        def _routed(findings: list[WedgeFinding]) -> list[str]:
            calls["routed"].append(("", {"n": len(findings)}))
            return []

        def _sweep(*_a: object, **_k: object) -> None:
            calls["sweep"].append(("", {}))

        monkeypatch.setattr(
            "cw.doctor.wedge.reap.has_pending_routed_result_audits", _pending
        )
        monkeypatch.setattr("cw.doctor.wedge.reap.reap_routed_result_findings", _routed)
        monkeypatch.setattr("cw.doctor.wedge.reap.sweep_leaked_daemon_workers", _sweep)
        return calls

    @staticmethod
    def _findings() -> list[WedgeFinding]:
        return [
            _finding(_WEDGE_LEAKED_DAEMON_WORKER, daemon_short_id="leak1"),
            _finding(WEDGE_ROUTED_RESULT_STRANDED, session_id="sess-routed"),
            _finding(_WEDGE_ACTIVE_NO_DAEMON_ENTRY, session_id=_OTHER_SID),
            _finding(_WEDGE_ACTIVE_NO_DAEMON_ENTRY, session_id=_ACME_SID),
            _finding(
                _WEDGE_ACTIVE_NO_DAEMON_ENTRY, ticket_id="ACM-2", session_id="sess-a2"
            ),
        ]

    @staticmethod
    def _seed() -> None:
        seed_queue(
            [],
            [
                _session(ACME, _ACME_SID),
                _session(OTHER, _OTHER_SID),
                _session(ACME, "sess-a2", "ACM-2"),
            ],
        )

    def test_scoped_reap_reaches_no_global_mutation(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_daemon_roster(tmp_path, monkeypatch)
        self._seed()
        calls = self._wire(monkeypatch)
        selector = _record_selector(monkeypatch)

        result = _reap_wedge_findings(self._findings(), scope=SCOPE)

        assert calls == {"pending": [], "routed": [], "sweep": []}
        assert [sid for sid, _ in selector] == [_ACME_SID]
        assert result is not None
        assert result.name == _SKIP_CHECK
        assert f"{_OTHER_SID} (out of scope: client {OTHER})" in result.detail

    def test_unscoped_control_reaches_every_global_mutation(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_daemon_roster(tmp_path, monkeypatch)
        self._seed()
        calls = self._wire(monkeypatch)
        selector = _record_selector(monkeypatch)

        result = _reap_wedge_findings(self._findings())

        assert result is None
        assert len(calls["pending"]) == 1
        assert len(calls["routed"]) == 1
        assert len(calls["sweep"]) == 1
        assert [sid for sid, _ in selector] == [_OTHER_SID, _ACME_SID, "sess-a2"]


class TestTicketPrefilter:
    """The ticket-id pre-filter drops other tickets' findings up front.

    ``scope.covers`` guards the RUNNING loop on its own, but the class-5/7
    helpers only see ``client=``: without the pre-filter an ``acme`` ACM-2
    park would be collapsed or cancelled by an ACM-2 finding.
    """

    _OTHER_TICKET = "ACM-2"

    def test_other_ticket_findings_mutate_nothing_and_save_nothing(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue(
            [
                make_task(
                    ticket_id=self._OTHER_TICKET,
                    status=QueueItemStatus.RUNNING,
                    session_id="s-2",
                    created_at=_at(0),
                ),
                make_blocked(
                    ticket_id=self._OTHER_TICKET,
                    session_id=None,
                    created_at=_at(1),
                ),
                make_blocked(
                    ticket_id=self._OTHER_TICKET,
                    session_id=None,
                    attempts=0,
                    disposition=ReapReason.TERMINAL_SIBLING.value,
                    created_at=_at(2),
                ),
            ]
        )
        before = [(t.status, t.session_id) for t in load_dev_queue().tasks]
        saves = _record_saves(monkeypatch)

        result = _reap_wedge_findings(
            [
                _finding(_TASK_RUNNING_NO_SESSION, ticket_id=self._OTHER_TICKET),
                _finding(_WEDGE_BLOCKED_DEAD_SESSION, ticket_id=self._OTHER_TICKET),
                _finding(_WEDGE_TERMINAL_SIBLING, ticket_id=self._OTHER_TICKET),
            ],
            scope=SCOPE,
        )

        assert result is None
        assert saves == []
        assert [(t.status, t.session_id) for t in load_dev_queue().tasks] == before
        assert before == [
            (QueueItemStatus.RUNNING, "s-2"),
            (QueueItemStatus.BLOCKED_ON_USER, None),
            (QueueItemStatus.BLOCKED_ON_USER, None),
        ]


class TestSessionRecordClient:
    """Class-6/8 client comes from the session record, not the finding (R5)."""

    @staticmethod
    def _findings() -> list[WedgeFinding]:
        return [
            _finding(_WEDGE_ACTIVE_NO_DAEMON_ENTRY, session_id=_ACME_SID),
            _finding(_WEDGE_ACTIVE_NO_DAEMON_ENTRY, session_id=_OTHER_SID),
        ]

    def test_only_the_in_scope_record_is_reaped(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([], [_session(ACME, _ACME_SID), _session(OTHER, _OTHER_SID)])
        selector = _record_selector(monkeypatch)

        _reap_wedge_findings(self._findings(), scope=SCOPE)

        assert [sid for sid, _ in selector] == [_ACME_SID]

    def test_unscoped_control_reaps_both(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Characterizes CURRENT unscoped behavior (#2928)."""
        seed_queue([], [_session(ACME, _ACME_SID), _session(OTHER, _OTHER_SID)])
        selector = _record_selector(monkeypatch)

        _reap_wedge_findings(self._findings())

        assert [sid for sid, _ in selector] == [_ACME_SID, _OTHER_SID]

    def test_foreign_client_session_is_skipped_reported_and_left_active(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_daemon_roster(tmp_path, monkeypatch)
        seed_queue([], [_session(ACME, _ACME_SID), _session(OTHER, _OTHER_SID)])

        result = _reap_wedge_findings(self._findings(), scope=SCOPE)

        assert result is not None
        assert result.name == _SKIP_CHECK
        assert result.ok is True
        assert result.warn is True
        assert result.detail == f"skipped: {_OTHER_SID} (out of scope: client {OTHER})"
        assert _session_status(_ACME_SID) == SessionStatus.COMPLETED
        assert _session_status(_OTHER_SID) == SessionStatus.ACTIVE

    @pytest.mark.parametrize(
        "exc",
        [OSError("disk gone"), json.JSONDecodeError("bad", "{", 0)],
        ids=["oserror", "json"],
    )
    def test_unreadable_state_skips_every_session(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        exc: Exception,
    ) -> None:
        seed_queue([_running(ACME)])
        selector = _record_selector(monkeypatch)

        def _raise() -> object:
            raise exc

        monkeypatch.setattr("cw.doctor.wedge.reap.load_state", _raise)

        result = _reap_wedge_findings(
            [
                _finding(_WEDGE_ACTIVE_NO_DAEMON_ENTRY, session_id=_ACME_SID),
                _finding(_TASK_RUNNING_NO_SESSION),
            ],
            scope=SCOPE,
        )

        assert result is not None
        assert result.name == _SKIP_CHECK
        assert result.ok is True
        assert result.warn is True
        assert _ACME_SID in result.detail
        assert "unreadable record" in result.detail
        assert selector == []
        assert _disk_statuses() == [(ACME, QueueItemStatus.PENDING)]

    def test_missing_record_is_treated_as_unreadable(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([], [_session(ACME, _ACME_SID)])
        selector = _record_selector(monkeypatch)

        result = _reap_wedge_findings(
            [_finding(_WEDGE_ACTIVE_NO_DAEMON_ENTRY, session_id="sess-ghost")],
            scope=SCOPE,
        )

        assert result is not None
        assert result.name == _SKIP_CHECK
        assert result.detail == "skipped: sess-ghost (unreadable record)"
        assert selector == []

    def test_skip_note_is_folded_into_a_timeout(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([], [_session(ACME, _ACME_SID), _session(OTHER, _OTHER_SID)])
        _record_selector(monkeypatch, raise_on={_ACME_SID})

        result = _reap_wedge_findings(self._findings(), scope=SCOPE)

        assert result is not None
        assert result.name == "wedge-reap"
        assert result.ok is False
        assert "sessions lock held" in result.detail
        assert result.detail.endswith(
            f"; skipped: {_OTHER_SID} (out of scope: client {OTHER})"
        )

    def test_scoped_lock_timeout_without_skips_still_fails(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([], [_session(ACME, _ACME_SID)])
        _record_selector(monkeypatch, raise_on={_ACME_SID})

        result = _reap_wedge_findings(
            [_finding(_WEDGE_ACTIVE_NO_DAEMON_ENTRY, session_id=_ACME_SID)],
            scope=SCOPE,
        )

        assert result is not None
        assert result.name == "wedge-reap"
        assert result.ok is False
        assert "skipped:" not in result.detail

    def test_findings_are_derived_before_the_queue_lock(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An uncaught state-read error leaves no partial scoped reap."""
        seed_queue([_running(ACME)])
        saves = _record_saves(monkeypatch)

        def _raise() -> object:
            msg = "unexpected"
            raise RuntimeError(msg)

        monkeypatch.setattr("cw.doctor.wedge.reap.load_state", _raise)

        with pytest.raises(RuntimeError):
            _reap_wedge_findings(
                [
                    _finding(_TASK_RUNNING_NO_SESSION),
                    _finding(_WEDGE_ACTIVE_NO_DAEMON_ENTRY, session_id=_ACME_SID),
                ],
                scope=SCOPE,
            )

        assert saves == []
        assert _disk_statuses() == [(ACME, QueueItemStatus.RUNNING)]


class TestTimeoutRerunHint:
    """The timeout's re-run hint names the scoped command when scoped (#2716)."""

    _UNSCOPED_HINT = "re-run `cw doctor --reap` once the holder releases"

    @staticmethod
    def _timeout_check(lock_dir: Path, scope: ReapScope | None = None) -> CheckResult:
        return _reap_timeout_check(
            _lock_timeout(lock_dir),
            reaped=[],
            not_found=[],
            remaining=[_ACME_SID],
            routed_status=None,
            swept=False,
            queue_saved=False,
            scope=scope,
        )

    def test_scoped_reap_timeout_hint_names_the_scoped_command(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([], [_session(ACME, _ACME_SID)])
        _record_selector(monkeypatch, raise_on={_ACME_SID})

        result = _reap_wedge_findings(
            [_finding(_WEDGE_ACTIVE_NO_DAEMON_ENTRY, session_id=_ACME_SID)],
            scope=SCOPE,
        )

        assert result is not None
        assert result.name == "wedge-reap"
        assert f"--ticket {ACME_TICKET}" in result.detail
        assert f"-c {ACME}" in result.detail
        assert self._UNSCOPED_HINT not in result.detail
        assert result.detail.endswith(
            f"re-run `cw doctor --reap --ticket {ACME_TICKET} -c {ACME}` "
            "once the holder releases"
        )

    def test_scoped_hint_survives_the_skip_note(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([], [_session(ACME, _ACME_SID), _session(OTHER, _OTHER_SID)])
        _record_selector(monkeypatch, raise_on={_ACME_SID})

        result = _reap_wedge_findings(
            [
                _finding(_WEDGE_ACTIVE_NO_DAEMON_ENTRY, session_id=_ACME_SID),
                _finding(_WEDGE_ACTIVE_NO_DAEMON_ENTRY, session_id=_OTHER_SID),
            ],
            scope=SCOPE,
        )

        assert result is not None
        assert f"--ticket {ACME_TICKET} -c {ACME}" in result.detail
        assert self._UNSCOPED_HINT not in result.detail

    def test_unscoped_hint_text_is_unchanged(self, tmp_path: Path) -> None:
        check = self._timeout_check(tmp_path)

        assert check.detail.endswith(f"; {self._UNSCOPED_HINT}")
        assert "--ticket" not in check.detail

    def test_scoped_hint_text_direct(self, tmp_path: Path) -> None:
        check = self._timeout_check(tmp_path, SCOPE)

        assert check.detail.endswith(
            f"; re-run `cw doctor --reap --ticket {ACME_TICKET} -c {ACME}` "
            "once the holder releases"
        )


class TestNoteSkippedSessions:
    def test_empty_skip_list_returns_input_unchanged(self) -> None:
        check = CheckResult("wedge-reap", ok=False, detail="timeout")

        assert _note_skipped_sessions(None, []) is None
        assert _note_skipped_sessions(check, []) is check


class TestScopeThreading:
    """``scope`` reaches the selector as ``collapse_client`` (R5)."""

    def test_scoped_daemon_reap_passes_collapse_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        selector = _record_selector(monkeypatch)

        _reap_daemon_sessions([(_ACME_SID, _WEDGE_ACTIVE_NO_DAEMON_ENTRY)], scope=SCOPE)

        assert selector == [
            (
                _ACME_SID,
                {
                    "proposed_action": _WEDGE_ACTIVE_NO_DAEMON_ENTRY,
                    "bounded": True,
                    "collapse_client": ACME,
                },
            )
        ]

    def test_unscoped_daemon_reap_kwargs_are_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        selector = _record_selector(monkeypatch)

        _reap_daemon_sessions([(_ACME_SID, _WEDGE_ACTIVE_NO_DAEMON_ENTRY)])

        assert selector == [
            (
                _ACME_SID,
                {"proposed_action": _WEDGE_ACTIVE_NO_DAEMON_ENTRY, "bounded": True},
            )
        ]

    def test_sessions_and_sweep_forwards_the_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[object] = []

        def _fake(
            findings: list[tuple[str, str]], *, scope: ReapScope | None = None
        ) -> tuple[list[str], list[str], list[str], None]:
            seen.append(scope)
            return [], [], [], None

        monkeypatch.setattr("cw.doctor.wedge.reap._reap_daemon_sessions", _fake)

        result = _reap_sessions_and_sweep(
            [],
            [],
            run_routed=False,
            has_leaked=False,
            queue_saved=False,
            scope=SCOPE,
        )

        assert result is None
        assert seen == [SCOPE]


class TestSelectorCollapseClient:
    """``_reap_session_by_selector(collapse_client=)`` (R5)."""

    @staticmethod
    def _seed() -> None:
        seed_queue(
            [_blocked(ACME, 0), _blocked(OTHER, 1)],
            [_session(ACME, _ACME_SID)],
        )

    def test_collapse_client_leaves_other_client_untouched(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_daemon_roster(tmp_path, monkeypatch)
        self._seed()

        assert _reap_session_by_selector(_ACME_SID, collapse_client=ACME)

        assert _disk_statuses() == [
            (ACME, QueueItemStatus.PENDING),
            (OTHER, QueueItemStatus.BLOCKED_ON_USER),
        ]

    def test_omitted_keyword_collapses_both_clients(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Characterizes CURRENT behavior: ticket-id-only collapse (#2928)."""
        stub_daemon_roster(tmp_path, monkeypatch)
        self._seed()

        assert _reap_session_by_selector(_ACME_SID)

        assert _disk_statuses() == [
            (ACME, QueueItemStatus.PENDING),
            (OTHER, QueueItemStatus.CANCELLED),
        ]


def _seed_two_client_wedges() -> None:
    """Every reapable class-2/5/6/7 condition, under both clients."""
    seed_queue(
        [
            _running(ACME),
            _running(OTHER),
            _blocked(ACME, 1),
            _blocked(OTHER, 2),
            _sibling(ACME, 3),
            _sibling(OTHER, 4),
        ],
        [_session(ACME, _ACME_SID), _session(OTHER, _OTHER_SID)],
    )


class TestScopedRunDoctor:
    _GLOBAL_TARGETS = (
        "cw.doctor.linkage.reconcile",
        "cw.doctor.core._check_wedge_repo_ahead",
        "cw.doctor.wedge.task_running.run_git",
        "cw.doctor.core.has_pending_routed_result_audits",
        "cw.doctor.wedge.reap.has_pending_routed_result_audits",
        "cw.doctor.wedge.reap.reap_routed_result_findings",
        "cw.doctor.wedge.reap.sweep_leaked_daemon_workers",
    )

    def test_scoped_run_changes_only_the_named_ticket(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_daemon_roster(tmp_path, monkeypatch)
        _seed_two_client_wedges()
        _raise_if_called(monkeypatch, *self._GLOBAL_TARGETS)

        report = run_doctor(reap=True, scope=SCOPE)

        assert _disk_statuses() == [
            (ACME, QueueItemStatus.PENDING),
            (OTHER, QueueItemStatus.RUNNING),
            (ACME, QueueItemStatus.PENDING),
            (OTHER, QueueItemStatus.BLOCKED_ON_USER),
            (ACME, QueueItemStatus.CANCELLED),
            (OTHER, QueueItemStatus.BLOCKED_ON_USER),
        ]
        assert _session_status(_ACME_SID) == SessionStatus.COMPLETED
        assert _session_status(_OTHER_SID) == SessionStatus.ACTIVE
        assert report.ok is True

        # One finding per seeded in-scope condition, none from `other`.
        classes = sorted(f.wedge_class for f in report.wedge_findings)
        assert classes == sorted(_SEEDED_ACME_CLASSES)
        assert {f.ticket_id for f in report.wedge_findings} == {ACME_TICKET}

        data = json.loads(format_report_json(report))
        assert {f["ticket_id"] for f in data["wedge_findings"]} == {ACME_TICKET}
        checks = {c["name"]: c for c in data["checks"]}
        assert checks["sessions.json"]["ok"] is True
        for name in _RAN_CHECKS:
            assert checks[name]["ok"] is True
            assert checks[name]["detail"].startswith("ran:")
        for name in _SKIPPED_CHECKS:
            assert checks[name]["ok"] is True
            assert checks[name]["detail"].startswith("skipped:")
        assert _SKIP_CHECK not in checks
        # A reap that completed keeps the session-reap markers at `ran:` (C1).
        for name in _SESSION_REAP_CHECKS:
            assert checks[name]["detail"].startswith("ran: ")
            assert "wedge-reap" not in checks[name]["detail"]
        assert "wedge-reap" not in checks

        # The report echoes the resolved scope and the matched count (F2).
        scope_check = checks[_SCOPE_CHECK]
        assert scope_check["ok"] is True
        assert scope_check["warn"] is False
        assert f"{ACME}/{ACME_TICKET}" in scope_check["detail"]
        assert (
            f"{len(report.wedge_findings)} wedge finding(s) matched"
            in (scope_check["detail"])
        )
        names = [c["name"] for c in data["checks"]]
        assert names.index("sessions.json") < names.index(_SCOPE_CHECK)
        assert names.index(_SCOPE_CHECK) < names.index(_RAN_CHECKS[0])

    def test_unscoped_report_has_no_scoped_reap_checks(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_daemon_roster(tmp_path, monkeypatch)
        monkeypatch.setattr("cw.doctor.core._check_wedge_repo_ahead", lambda *_a: [])

        report = run_doctor()

        names = [c["name"] for c in json.loads(format_report_json(report))["checks"]]
        assert not [n for n in names if n.startswith("scoped-reap/")]

    def test_unreadable_sessions_json_stops_before_any_reap(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([_running(ACME)])
        state_file().parent.mkdir(parents=True, exist_ok=True)
        state_file().write_text("{not json", encoding="utf-8")
        saves = _record_saves(monkeypatch)

        report = run_doctor(reap=True, scope=SCOPE)

        state_check = next(c for c in report.checks if c.name == "sessions.json")
        assert state_check.ok is False
        assert report.ok is False
        assert not [c for c in report.checks if c.name in _RAN_CHECKS]
        assert not [c for c in report.checks if c.name == _SCOPE_CHECK]
        assert saves == []
        assert _disk_statuses() == [(ACME, QueueItemStatus.RUNNING)]

    def test_no_match_is_an_ok_no_op(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_daemon_roster(tmp_path, monkeypatch)
        _seed_two_client_wedges()
        saves = _record_saves(monkeypatch)
        before = _disk_statuses()

        report = run_doctor(reap=True, scope=ReapScope(ACME, "ACM-9"))

        assert report.ok is True
        assert report.wedge_findings == []
        assert saves == []
        assert _disk_statuses() == before
        # A zero-match run is visibly [WARN] but still an ok report (exit 0).
        scope_check = next(c for c in report.checks if c.name == _SCOPE_CHECK)
        assert scope_check.ok is True
        assert scope_check.warn is True
        assert "0 wedge finding(s)" in scope_check.detail
        assert f"{ACME}/ACM-9" in scope_check.detail
        # The reap was never invoked, so no marker may claim a mutation ran.
        checks = {c.name: c for c in report.checks}
        for name in _RAN_CHECKS:
            assert checks[name].ok is True
            assert checks[name].detail.startswith(_NO_MATCH_PREFIX)
            assert not checks[name].detail.startswith("ran:")
        for name in _SKIPPED_CHECKS:
            assert checks[name].detail.startswith("skipped:")

    def test_scope_without_reap_is_a_misuse(self, tmp_config_dir: Path) -> None:
        with pytest.raises(ValueError, match="reap"):
            run_doctor(reap=False, scope=SCOPE)

    def test_scoped_lock_timeout_is_a_failing_check(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_daemon_roster(tmp_path, monkeypatch)
        _seed_two_client_wedges()
        _record_selector(monkeypatch, raise_on={_ACME_SID})

        report = run_doctor(reap=True, scope=SCOPE)

        reap_check = next(c for c in report.checks if c.name == "wedge-reap")
        assert reap_check.ok is False
        assert report.ok is False

    def test_lock_timeout_marks_the_session_reap_markers_incomplete(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A timed-out session tail must not read ``ran: ... reaped`` (C1)."""
        stub_daemon_roster(tmp_path, monkeypatch)
        _seed_two_client_wedges()
        _record_selector(monkeypatch, raise_on={_ACME_SID})

        report = run_doctor(reap=True, scope=SCOPE)

        checks = {c.name: c for c in report.checks}
        assert checks["wedge-reap"].ok is False
        for name in _SESSION_REAP_CHECKS:
            assert checks[name].ok is True
            assert checks[name].detail.startswith(_INCOMPLETE_PREFIX)
            assert not checks[name].detail.startswith("ran:")
        for name in _QUEUE_SIDE_CHECKS:
            assert checks[name].detail.startswith("ran:")
        for name in _SKIPPED_CHECKS:
            assert checks[name].detail.startswith("skipped:")


class TestScopedClass8:
    """Class 8 (daemon-stale, no sentinel) on the scoped path (#2716)."""

    @staticmethod
    def _seed_stale_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An ACTIVE, roster-live, stale-transcript session per client.

        Both sessions are named for ACM-1, so the unscoped detector flags both;
        each has a RUNNING row so the scoped queue is exercised too.
        """
        daemon = stub_daemon_roster(tmp_path, monkeypatch)
        sessions = []
        for client, sid in ((ACME, _ACME_SID), (OTHER, _OTHER_SID)):
            worktree = tmp_path / f"wt-{client}"
            ref = daemon.seed_live_worker(worktree)
            _stamp_transcript_age(
                Path.home(),
                worktree,
                stale_minutes=_STALE_MINUTES,
                surface_ref=ref,
            )
            sessions.append(
                _make_daemon_session(
                    id=sid,
                    client=client,
                    name=f"{client}/auto-dev/{ACME_TICKET}",
                    surface_ref=ref,
                    worktree_path=worktree,
                    status=SessionStatus.ACTIVE,
                )
            )
        seed_queue([_running(ACME, _ACME_SID), _running(OTHER, _OTHER_SID)], sessions)

    def test_collector_flags_only_the_scoped_clients_session(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed_stale_sessions(tmp_path, monkeypatch)
        state, queue = load_state(), load_dev_queue()

        control = _check_wedge_active_daemon_stale_no_sentinel(state, queue)
        assert sorted(f.session_id or "" for f in control) == [_ACME_SID, _OTHER_SID]

        findings = _collect_scoped_wedge_findings(state, queue, SCOPE)

        assert [(f.wedge_class, f.session_id, f.ticket_id) for f in findings] == [
            (_WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL, _ACME_SID, ACME_TICKET)
        ]

    def test_reap_collapses_with_the_scope_client_and_skips_the_foreign_one(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_daemon_roster(tmp_path, monkeypatch)
        seed_queue([], [_session(ACME, _ACME_SID), _session(OTHER, _OTHER_SID)])
        selector = _record_selector(monkeypatch)
        findings = [
            _finding(_WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL, session_id=sid)
            for sid in (_ACME_SID, _OTHER_SID)
        ]

        result = _reap_wedge_findings(findings, scope=SCOPE)

        assert selector == [
            (
                _ACME_SID,
                {
                    "proposed_action": _WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL,
                    "bounded": True,
                    "collapse_client": ACME,
                },
            )
        ]
        assert result is not None
        assert result.name == _SKIP_CHECK
        assert result.detail == f"skipped: {_OTHER_SID} (out of scope: client {OTHER})"


class TestScopedCollection:
    def test_detectors_do_not_mutate_the_loaded_state_or_queue(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_daemon_roster(tmp_path, monkeypatch)
        _seed_two_client_wedges()
        state = load_state()
        queue = load_dev_queue()
        state_before = state.model_dump()
        queue_before = queue.model_dump()

        findings = _collect_scoped_wedge_findings(state, queue, SCOPE)

        assert sorted(f.wedge_class for f in findings) == sorted(_SEEDED_ACME_CLASSES)
        assert state.model_dump() == state_before
        assert queue.model_dump() == queue_before

    def test_classes_2_3_5_see_the_full_state_not_the_scoped_one(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A live session whose name is not the scoped ticket is not "dead".

        The session is dropped from the scoped state (its name resolves to
        ACM-77), but the acme ACM-1 row bound to it still resolves through the
        full state. Passing the scoped state to the class-2 detector would
        make the live session look absent and report a false wedge.
        """
        stub_daemon_roster(tmp_path, monkeypatch)
        seed_queue(
            [_running(ACME, "sess-live")], [_session(ACME, "sess-live", "ACM-77")]
        )

        findings = _collect_scoped_wedge_findings(load_state(), load_dev_queue(), SCOPE)

        assert [f for f in findings if f.wedge_class == _TASK_RUNNING_NO_SESSION] == []
        assert findings == []

    def test_class5_live_session_of_another_ticket_is_not_dead(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Class 5 resolves a BLOCKED_ON_USER row's session through the FULL state.

        The acme ACM-1 row is bound to a live ACTIVE session named for ACM-77,
        which the scoped state drops. With the scoped state the detector would
        see no session record and report the live row as dead (a false wedge).
        The other client's same-ticket dead row is a control: it is a real
        class-5 shape unscoped but is outside the scope.
        """
        daemon = stub_daemon_roster(tmp_path, monkeypatch)
        live_ref = daemon.seed_live_worker(tmp_path / "wt-live")
        live = _make_daemon_session(
            id="sess-live",
            client=ACME,
            name=f"{ACME}/auto-dev/ACM-77",
            surface_ref=live_ref,
            status=SessionStatus.ACTIVE,
        )
        acme_row = make_blocked(client=ACME, session_id="sess-live", created_at=_at(1))
        other_row = make_blocked(client=OTHER, session_id=None, created_at=_at(2))
        seed_queue([acme_row, other_row], [live])
        state, queue = load_state(), load_dev_queue()

        control = _check_wedge_dead_session_blocked_on_user(state, queue)
        assert [f.ticket_id for f in control] == [ACME_TICKET]  # other's dead row

        findings = _collect_scoped_wedge_findings(state, queue, SCOPE)

        assert [
            f for f in findings if f.wedge_class == _WEDGE_BLOCKED_DEAD_SESSION
        ] == []

    def test_class3_completed_session_of_another_ticket_is_still_found(
        self, tmp_config_dir: Path
    ) -> None:
        """Class 3 resolves a RUNNING row's session through the FULL state.

        The acme ACM-1 row is bound to a COMPLETED session named for ACM-77.
        The scoped state drops that session, so a detector handed it would see
        no record and lose the finding. The other client's same-ticket row is
        the control that the scope still filters by client.
        """
        seed_queue(
            [_running(ACME, _ACME_SID), _running(OTHER, _OTHER_SID)],
            [
                _session(ACME, _ACME_SID, "ACM-77", SessionStatus.COMPLETED),
                _session(OTHER, _OTHER_SID, "ACM-77", SessionStatus.COMPLETED),
            ],
        )

        findings = _collect_scoped_wedge_findings(load_state(), load_dev_queue(), SCOPE)

        assert [(f.wedge_class, f.session_id, f.ticket_id) for f in findings] == [
            (_TASK_RUNNING_COMPLETED, _ACME_SID, ACME_TICKET)
        ]

    def test_class3_completed_session_is_collected_for_the_scoped_client_only(
        self, tmp_config_dir: Path
    ) -> None:
        seed_queue(
            [_running(ACME, _ACME_SID), _running(OTHER, _OTHER_SID)],
            [
                _session(ACME, _ACME_SID, status=SessionStatus.COMPLETED),
                _session(OTHER, _OTHER_SID, status=SessionStatus.COMPLETED),
            ],
        )

        findings = _collect_scoped_wedge_findings(load_state(), load_dev_queue(), SCOPE)

        assert [(f.wedge_class, f.session_id, f.ticket_id) for f in findings] == [
            (_TASK_RUNNING_COMPLETED, _ACME_SID, ACME_TICKET)
        ]

    def test_class12_occupied_worktree_is_collected_for_the_scoped_client_only(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
        start = (
            now
            - timedelta(seconds=10)
            - (PENDING_SKIP_WEDGE_THRESHOLD - 1) * (timedelta(seconds=30))
        )
        events = [
            event
            for client in (ACME, OTHER)
            for event in skip_tick_events(
                client,
                ACME_TICKET,
                "worktree_occupied",
                count=PENDING_SKIP_WEDGE_THRESHOLD,
                start=start,
            )
        ]
        monkeypatch.setattr(
            "cw.doctor.pending_skip_wedge.read_events", lambda **_kw: events
        )
        seed_queue(
            [
                make_task(
                    client=client,
                    status=QueueItemStatus.PENDING,
                    created_at=now - timedelta(days=1),
                )
                for client in (ACME, OTHER)
            ]
        )

        with freeze_time(now):
            findings = _collect_scoped_wedge_findings(
                load_state(), load_dev_queue(), SCOPE
            )

        (finding,) = findings
        assert isinstance(finding, PendingWorktreeOccupiedFinding)
        assert finding.wedge_class == WEDGE_PENDING_WORKTREE_OCCUPIED
        assert (finding.client, finding.ticket_id) == (ACME, ACME_TICKET)


class TestScopedRecipes:
    """A scoped report's finding recipes name the scoped command (#2716).

    The detectors write ``Run: cw doctor --reap ...``; a reader following that
    line from a scoped report would run the GLOBAL reap on every client.
    """

    @staticmethod
    def _assert_scoped(recipe: str) -> None:
        assert SCOPE.reap_command() in recipe
        assert _GLOBAL_RECIPE_TAIL not in recipe
        assert recipe.replace(SCOPE.reap_command(), "").count(REAP_COMMAND) == 0, recipe

    def test_run_doctor_recipes_name_the_scoped_command(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_daemon_roster(tmp_path, monkeypatch)
        _seed_two_client_wedges()

        report = run_doctor(reap=True, scope=SCOPE)

        # Classes 2, 5, 7 and 6: every seeded condition, each with a recipe
        # that named the global command before the rewrite.
        assert sorted(f.wedge_class for f in report.wedge_findings) == sorted(
            _SEEDED_ACME_CLASSES
        )
        for finding in report.wedge_findings:
            self._assert_scoped(finding.recipe)
        data = json.loads(format_report_json(report))
        assert len(data["wedge_findings"]) == len(_SEEDED_ACME_CLASSES)
        for entry in data["wedge_findings"]:
            self._assert_scoped(entry["recipe"])

    def test_class8_recipe_names_the_scoped_command(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        TestScopedClass8._seed_stale_sessions(tmp_path, monkeypatch)

        findings = _collect_scoped_wedge_findings(load_state(), load_dev_queue(), SCOPE)

        (finding,) = findings
        assert finding.wedge_class == _WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL
        self._assert_scoped(finding.recipe)

    def test_class12_recipe_names_the_scoped_command(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
        start = (
            now
            - timedelta(seconds=10)
            - (PENDING_SKIP_WEDGE_THRESHOLD - 1) * (timedelta(seconds=30))
        )
        events = skip_tick_events(
            ACME,
            ACME_TICKET,
            "worktree_occupied",
            count=PENDING_SKIP_WEDGE_THRESHOLD,
            start=start,
        )
        monkeypatch.setattr(
            "cw.doctor.pending_skip_wedge.read_events", lambda **_kw: events
        )
        seed_queue(
            [
                make_task(
                    client=ACME,
                    status=QueueItemStatus.PENDING,
                    created_at=now - timedelta(days=1),
                )
            ]
        )

        with freeze_time(now):
            (finding,) = _collect_scoped_wedge_findings(
                load_state(), load_dev_queue(), SCOPE
            )

        assert isinstance(finding, PendingWorktreeOccupiedFinding)
        assert finding.client == ACME
        assert finding.recipe.endswith(f"and {SCOPE.reap_command()}.")

    def test_unscoped_recipes_are_unchanged(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Characterization control: the unscoped collector keeps its text."""
        stub_daemon_roster(tmp_path, monkeypatch)
        _seed_two_client_wedges()
        monkeypatch.setattr("cw.doctor.core._check_wedge_repo_ahead", lambda *_a: [])
        state, queue = load_state(), load_dev_queue()

        unscoped = _collect_wedge_findings(state, queue)
        scoped = _collect_scoped_wedge_findings(state, queue, SCOPE)

        assert unscoped
        for finding in unscoped:
            assert "--ticket" not in finding.recipe
        assert any(_GLOBAL_RECIPE_TAIL in f.recipe for f in unscoped)
        # Task-running recipe text, byte for byte (task_running.py).
        assert any(
            f.wedge_class == _TASK_RUNNING_NO_SESSION
            and f.recipe.endswith("Run: cw doctor --reap to revert task to PENDING.")
            for f in unscoped
        )
        # Each scoped recipe is its unscoped twin with only the phrase swapped.
        by_key = {(f.wedge_class, f.session_id): f for f in unscoped if f.ticket_id}
        for finding in scoped:
            twin = by_key[(finding.wedge_class, finding.session_id)]
            assert twin.recipe.replace(REAP_COMMAND, SCOPE.reap_command()) == (
                finding.recipe
            )

    def test_rewrite_leaves_a_recipe_without_the_phrase_unchanged(self) -> None:
        finding = _finding(_TASK_RUNNING_NO_SESSION)

        assert _scope_finding_recipe(finding, SCOPE) == finding

    def test_rewrite_replaces_only_the_exact_command_phrase(self) -> None:
        finding = WedgeFinding(
            wedge_class=_TASK_RUNNING_NO_SESSION,
            session_id="sid",
            ticket_id=ACME_TICKET,
            recipe=(
                "Run: cw doctor --reap, or cw doctor --reap-all (not ours), "
                "then cw doctor --reaped."
            ),
            state_file="state",
            daemon_short_id="abc",
        )

        result = _scope_finding_recipe(finding, SCOPE)

        assert result.recipe == (
            f"Run: {SCOPE.reap_command()}, or cw doctor --reap-all (not ours), "
            "then cw doctor --reaped."
        )
        assert result.recipe.count("--ticket") == 1

    def test_rewrite_quotes_the_scope_for_the_shell(self) -> None:
        scope = ReapScope("my client", "AB-1; rm -rf x")
        finding = WedgeFinding(
            wedge_class=_TASK_RUNNING_NO_SESSION,
            session_id=None,
            ticket_id=ACME_TICKET,
            recipe="Run: cw doctor --reap to fix.",
            state_file="state",
        )

        result = _scope_finding_recipe(finding, scope)

        assert result.recipe == (
            "Run: cw doctor --reap --ticket 'AB-1; rm -rf x' -c 'my client' to fix."
        )

    def test_rewrite_preserves_every_other_field(self) -> None:
        finding = WedgeFinding(
            wedge_class=_WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL,
            session_id="sid",
            ticket_id=ACME_TICKET,
            recipe="Run: cw doctor --reap to stop it.",
            state_file="state-file",
            daemon_short_id="abc123",
        )

        result = _scope_finding_recipe(finding, SCOPE)

        assert (
            result.wedge_class,
            result.session_id,
            result.ticket_id,
            result.state_file,
            result.daemon_short_id,
        ) == (
            finding.wedge_class,
            "sid",
            ACME_TICKET,
            "state-file",
            "abc123",
        )
        assert result.recipe != finding.recipe

    def test_timeout_hint_quotes_the_scope(self, tmp_path: Path) -> None:
        scope = ReapScope("my client", ACME_TICKET)

        check = TestTimeoutRerunHint._timeout_check(tmp_path, scope)

        assert check.detail.endswith(
            f"; re-run `cw doctor --reap --ticket {ACME_TICKET} -c 'my client'` "
            "once the holder releases"
        )


# Drift guard (#2716). The detectors hard-code the global reap phrase in their
# recipes; the scoped rewrite (`_GLOBAL_REAP_PHRASE`) only works while every
# literal stays byte-equal to `REAP_COMMAND`. This walks the detector SOURCE, so
# a respelled, re-wrapped or newly added recipe fails here whether or not a
# `TestScopedRecipes` case seeds its class.
_DOCTOR_PKG = Path(cw.doctor.__file__).parent
# Deliberately looser than `REAP_COMMAND`, so a respelled phrase (`cw  doctor
# --reap`) is still SELECTED and then fails the equality assertion below.
_LOOSE_REAP_PHRASE = re.compile(r"doctor\s+--\s*reap")
# Lower bound taken from the tree at #2716: ten recipe literals across
# task_running, blocked_on_user, session_liveness, orphans, pending_skip_wedge
# and routed_result_wedge. A glob or pattern that matches nothing must not pass.
_MIN_RECIPE_LITERALS = 10


def _recipe_literals() -> list[tuple[str, str]]:
    """``(path:line, text)`` of every detector recipe literal under ``cw.doctor``.

    ``ast.Constant`` folds implicit string concatenation, so a phrase split
    mid-way across source lines is seen as one string. Excluded by explicit
    rule: ``_shared.py`` (defines ``REAP_COMMAND``), docstrings, and logging
    format strings (contain ``%s``; operator-facing log text, not a recipe).
    """
    found: list[tuple[str, str]] = []
    for path in sorted(_DOCTOR_PKG.rglob("*.py")):
        if path.name == "_shared.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(
                node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and "%s" not in node.value
                and _LOOSE_REAP_PHRASE.search(node.value)
            ):
                where = f"{path.relative_to(_DOCTOR_PKG)}:{node.lineno}"
                found.append((where, node.value))
    return found


_RECIPE_LITERALS = _recipe_literals()


class TestRecipeLiteralDriftGuard:
    """Every detector recipe literal still matches the scoped rewrite (#2716)."""

    def test_walk_finds_the_known_recipe_literals(self) -> None:
        assert len(_RECIPE_LITERALS) >= _MIN_RECIPE_LITERALS, _RECIPE_LITERALS

    @pytest.mark.parametrize(
        ("where", "text"), _RECIPE_LITERALS, ids=[w for w, _ in _RECIPE_LITERALS]
    )
    def test_literal_names_the_exact_global_phrase_and_rewrites(
        self, where: str, text: str
    ) -> None:
        assert REAP_COMMAND in text, f"{where}: respelled global phrase: {text!r}"
        finding = WedgeFinding(
            wedge_class=_TASK_RUNNING_NO_SESSION,
            session_id=None,
            ticket_id=ACME_TICKET,
            recipe=text,
            state_file="state",
        )

        rewritten = _scope_finding_recipe(finding, SCOPE).recipe

        assert rewritten != text, f"{where}: rewrite did not fire: {text!r}"
        assert SCOPE.reap_command() in rewritten
        assert REAP_COMMAND not in rewritten.replace(SCOPE.reap_command(), ""), (
            f"{where}: bare global phrase left behind: {rewritten!r}"
        )
