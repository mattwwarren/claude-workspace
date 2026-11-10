"""Characterization tests for the claim-time SESSION_NEEDS_ATTENTION emitters.

Each emitter in :mod:`cw.dispatch.claim.events` owns a pre-dispatch park
payload. The expected ordered ``(key, value)`` pairs below were captured by
running each emitter against the pre-refactor source; they pin key order,
values and the absence of extra keys, so a refactor that changes any of them
fails here. The shared builder class at the bottom exercises
``_record_pre_dispatch_attention_event`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.events import read_events
from cw.models import CLAUDE_NATIVE_BACKEND, OPENCODE_BACKEND, OrchestratorEventType
from tests._adopt_pr_helpers import make_adopted_task

if TYPE_CHECKING:
    from cw.events import OrchestratorEvent

_CLIENT = "test-client"
_LANE = "lane-a"


def _only_event() -> OrchestratorEvent:
    """Return the single SESSION_NEEDS_ATTENTION event recorded this test."""
    events = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
    assert len(events) == 1
    return events[0]


def _common(*, session_id: str, paused_status: object) -> list[tuple[str, object]]:
    """The nine common fields in their fixed order, for ``ticket_id="T-1"``."""
    return [
        ("session_id", session_id),
        ("session_name", ""),
        ("client", _CLIENT),
        ("ticket_id", "T-1"),
        ("claude_session_id", None),
        ("paused_status", paused_status),
        ("breadcrumbs", ""),
        ("crashed", False),
        ("lane", _LANE),
    ]


class TestAttemptCapAttention:
    def test_payload_pins_common_fields_then_attempt_ceiling(self) -> None:
        from cw.dispatch.claim.events import _emit_attempt_cap_attention_event

        task = make_adopted_task()
        _emit_attempt_cap_attention_event(task, _CLIENT, _LANE, 7)

        ev = _only_event()
        assert ev.type == OrchestratorEventType.SESSION_NEEDS_ATTENTION
        assert ev.correlation_id == "T-1"
        assert list(ev.payload.items()) == [
            *_common(session_id="", paused_status="attempt_cap_blocked"),
            ("attempt_ceiling", 7),
        ]


class TestStaleDispatchAttention:
    def test_payload_carries_session_id_and_no_extras(self) -> None:
        from cw.dispatch.claim.events import _emit_stale_dispatch_attention_event

        task = make_adopted_task(session_id="sess-1")
        _emit_stale_dispatch_attention_event(task, _CLIENT, _LANE)

        ev = _only_event()
        assert ev.type == OrchestratorEventType.SESSION_NEEDS_ATTENTION
        assert ev.correlation_id == "T-1"
        assert list(ev.payload.items()) == _common(
            session_id="sess-1", paused_status="stale_dispatch_gate"
        )


class TestTrackerMcpGateAttention:
    def test_payload_pins_common_fields_then_gate_extras(self) -> None:
        from cw.dispatch.claim.events import _emit_tracker_mcp_gate_attention_event

        task = make_adopted_task()
        _emit_tracker_mcp_gate_attention_event(
            task,
            _CLIENT,
            _LANE,
            branch="B",
            file_inspected="F",
            expected="P",
            backend=CLAUDE_NATIVE_BACKEND,
        )

        ev = _only_event()
        assert ev.type == OrchestratorEventType.SESSION_NEEDS_ATTENTION
        assert ev.correlation_id == "T-1"
        assert list(ev.payload.items()) == [
            *_common(session_id="", paused_status="tracker_mcp_gate"),
            ("branch", "B"),
            ("file_inspected", "F"),
            ("expected_plugin", "P"),
            ("backend", "claude-native"),
            ("details", "branch=B; file_inspected=F; expected_plugin=P"),
        ]

    def test_opencode_payload_names_the_server(self) -> None:
        """#3055: a non-Claude hit carries expected_server, not expected_plugin."""
        from cw.dispatch.claim.events import _emit_tracker_mcp_gate_attention_event

        task = make_adopted_task()
        _emit_tracker_mcp_gate_attention_event(
            task,
            _CLIENT,
            _LANE,
            branch="B",
            file_inspected="/h/.config/opencode/opencode.json",
            expected="linear",
            backend=OPENCODE_BACKEND,
        )

        ev = _only_event()
        assert list(ev.payload.items()) == [
            *_common(session_id="", paused_status="tracker_mcp_gate"),
            ("branch", "B"),
            ("file_inspected", "/h/.config/opencode/opencode.json"),
            ("backend", "opencode"),
            ("expected_server", "linear"),
            (
                "details",
                "branch=B; file_inspected=/h/.config/opencode/opencode.json;"
                " backend=opencode; expected_server=linear",
            ),
        ]


class TestReviewApprovalGateAttention:
    def test_payload_pins_common_fields_then_approval_extras(self) -> None:
        from cw.dispatch.claim.events import (
            _emit_review_approval_gate_attention_event,
        )

        task = make_adopted_task()
        approval_id = "TOK"
        _emit_review_approval_gate_attention_event(
            task, _CLIENT, _LANE, branch="B", token=approval_id, detail="DET"
        )

        ev = _only_event()
        assert ev.type == OrchestratorEventType.SESSION_NEEDS_ATTENTION
        assert ev.correlation_id == "T-1"
        assert list(ev.payload.items()) == [
            *_common(session_id="", paused_status="review_approval_gate"),
            ("branch", "B"),
            ("approval_token", "TOK"),
            (
                "details",
                "Step 4.0 would park this large-tier ticket (TOK: DET); the claim"
                " was refused before any worker started. Fix any environment fault"
                " named in the detail first, then release with `cw dev-queue"
                " requeue T-1 -c test-client --stage review` to obtain a fresh"
                " review approval. A plain finalize requeue re-parks.",
            ),
        ]


class TestAdoptionAttention:
    @pytest.mark.parametrize(
        ("paused_status", "observed_head_sha", "commit_range", "expected_observed"),
        [
            ("adopted_head_drift", "c" * 40, "x..y", "c" * 40),
            ("adopt_pr_state_unknown", None, "", None),
        ],
    )
    def test_payload_pins_common_fields_then_adoption_extras(
        self,
        paused_status: str,
        observed_head_sha: str | None,
        commit_range: str,
        expected_observed: str | None,
    ) -> None:
        from cw.dispatch.claim.events import emit_adoption_attention_event

        task = make_adopted_task()
        emit_adoption_attention_event(
            task,
            _CLIENT,
            _LANE,
            paused_status=paused_status,
            branch="B",
            observed_head_sha=observed_head_sha,
            commit_range=commit_range,
            details="D",
        )

        ev = _only_event()
        assert ev.type == OrchestratorEventType.SESSION_NEEDS_ATTENTION
        assert ev.correlation_id == "T-1"
        assert list(ev.payload.items()) == [
            *_common(session_id="", paused_status=paused_status),
            ("branch", "B"),
            ("pr_url", "https://github.com/acme/widgets/pull/7"),
            ("adopted_head_sha", "a" * 40),
            ("observed_head_sha", expected_observed),
            ("commit_range", commit_range),
            ("details", "D"),
        ]


class TestPreDispatchAttentionBuilder:
    @pytest.mark.parametrize("shadowed_key", ["lane", "session_id", "breadcrumbs"])
    def test_extra_shadowing_common_key_raises_and_writes_nothing(
        self, shadowed_key: str
    ) -> None:
        from cw.dispatch.claim.events import _record_pre_dispatch_attention_event

        task = make_adopted_task()
        with pytest.raises(ValueError, match="shadow common attention fields"):
            _record_pre_dispatch_attention_event(
                task,
                _CLIENT,
                _LANE,
                paused_status="x",
                extra={shadowed_key: "boom"},
            )

        assert (
            read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
            == []
        )

    def test_extra_none_emits_only_common_fields(self) -> None:
        from cw.dispatch.claim.events import _record_pre_dispatch_attention_event

        task = make_adopted_task()
        _record_pre_dispatch_attention_event(task, _CLIENT, _LANE, paused_status="x")

        ev = _only_event()
        assert list(ev.payload.items()) == _common(session_id="", paused_status="x")

    def test_extra_keys_follow_common_fields_in_insertion_order(self) -> None:
        from cw.dispatch.claim.events import _record_pre_dispatch_attention_event

        task = make_adopted_task()
        _record_pre_dispatch_attention_event(
            task,
            _CLIENT,
            _LANE,
            paused_status="x",
            extra={"zeta": 1, "alpha": 2},
        )

        ev = _only_event()
        assert list(ev.payload.items()) == [
            *_common(session_id="", paused_status="x"),
            ("zeta", 1),
            ("alpha", 2),
        ]
