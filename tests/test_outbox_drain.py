"""The outbox drain engine (#3706, RFC 0019 O2): one test per decision-table row.

Every ``gh`` call is answered by :class:`tests._shared.outbox_builders.GhThread`
(a stateful wrapper over ``FakeGhCli``); the read-back sleep is patched to 0.
Rows, markers and the held list are the plan's P3, P5 and P11.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time

from cw import outbox_drain
from cw.exceptions import CwError
from cw.gh import AGENT_COMMENT_MARKER, agent_marked_body
from cw.models import OrchestratorEventType
from cw.models.bindings import Bindings
from cw.outbox import (
    OutboxEntry,
    OutboxReason,
    OutboxRecordError,
    OutboxRole,
    OutboxState,
    _layout,
    claim,
    marked_roles,
    read_held,
    read_marker,
    settle,
)
from cw.outbox_drain import (
    DrainReport,
    GhTarget,
    McpTarget,
    part_marker_line,
    run_drain,
    select_target,
)
from cw.worktree import _git_dir
from tests._reconcile_helpers import live_launch_marker
from tests._shared.bindings_builders import bindings_linear_mcp
from tests._shared.daemon_fakes import CapturedEvent
from tests._shared.lock_helpers import _hold_flock
from tests._shared.outbox_builders import CLIENT, GhThread, client_config, enqueue

_ENGINE = Path(__file__).resolve().parent.parent / "src" / "cw" / "outbox_drain.py"
_PAYLOAD_KEYS = {
    "client",
    "ticket",
    "role",
    "marker",
    "producer",
    "entry_id",
    "reason",
    "posted",
    "held",
}
_STAMPS = [f"2026-11-0{day}T10:00:00Z" for day in range(1, 10)]


@pytest.fixture(autouse=True)
def _no_readback_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cw.tracker_post._READBACK_DELAY_SECONDS", 0.0)


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> GhThread:
    return GhThread().install(monkeypatch)


@pytest.fixture
def events(
    capture_events: Callable[..., list[CapturedEvent]],
) -> list[CapturedEvent]:
    return capture_events("cw.outbox_drain")


def _drain(
    tmp_path: Path,
    *,
    bindings: Bindings | None = None,
    limit: int | None = None,
    force: bool = False,
    tracker: str | None = None,
) -> DrainReport:
    cfg = client_config(tmp_path, bindings=bindings, tracker=tracker)
    return run_drain(CLIENT, cfg, limit=limit, force=force)


def _entry(entry_id: str) -> OutboxEntry:
    return _layout.read_record(CLIENT, entry_id)


def _state(entry_id: str) -> tuple[OutboxState, OutboxReason | None]:
    entry = _entry(entry_id)
    return entry.state, entry.reason


def _at(index: int, **kwargs: Any) -> OutboxEntry:
    """Enqueue at a fixed, ordered ``created_at`` so claim order is pinned."""
    with freeze_time(_STAMPS[index]):
        return enqueue(**kwargs).entry


def _unverified(index: int = 0, **kwargs: Any) -> OutboxEntry:
    """An entry already settled ``unverified/readback_unverified``."""
    entry = _at(index, **kwargs)
    (claimed,) = claim(CLIENT, exclude_ids=_others(entry.id))
    settle(
        CLIENT,
        claimed.id,
        claim_token=claimed.claim_token or "",
        state=OutboxState.UNVERIFIED,
        reason=OutboxReason.READBACK_UNVERIFIED,
    )
    return _entry(entry.id)


def _others(entry_id: str) -> frozenset[str]:
    return frozenset(e.id for e in _layout.scan_entries(CLIENT) if e.id != entry_id)


def _posted_text(part: str, marker: str, index: int, total: int) -> str:
    return f"{part}\n\n{part_marker_line(marker, index, total)}"


def _types(events: list[CapturedEvent]) -> list[OrchestratorEventType]:
    return [etype for etype, _payload, _cid in events]


def _raise_record_error(_entry: OutboxEntry) -> list[str]:
    msg = "body vanished"
    raise OutboxRecordError(msg)


class TestArgvPin:
    def test_posts_through_gh_issue_comment_with_the_marker_line(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        entry = enqueue(ticket="42", parts=("hello",)).entry

        _drain(tmp_path)

        (call,) = gh.comment_records()
        assert call.argv == ["gh", "issue", "comment", "42", "--body-file", "-"]
        assert call.cwd == _git_dir(client_config(tmp_path))
        expected = agent_marked_body("hello\n\n<!-- cw-outbox:m1:1/1 -->")
        assert call.stdin == expected.encode("utf-8")
        assert all(snapshot == () for snapshot in gh.gh.lock_snapshots)
        assert _state(entry.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)

    def test_select_target_requires_a_usable_tickets_binding(
        self, tmp_path: Path
    ) -> None:
        target = select_target(client_config(tmp_path))
        assert isinstance(target, GhTarget)
        assert target.cwd == tmp_path
        # O2b: a codex MCP realization is the courier's target, not unreachable.
        mcp = client_config(tmp_path, bindings=bindings_linear_mcp())
        assert select_target(mcp) == McpTarget(server="linear")
        # No codex realization (opencode only) stays unusable: G1.
        no_codex = bindings_linear_mcp(harnesses=("opencode",))
        assert select_target(client_config(tmp_path, bindings=no_codex)) is None


class TestSelectTargetFallback:
    """RFC 0019 O3 R9: no ``bindings.tickets`` block, by tracker polarity."""

    def test_no_bindings_github_tracker_targets_gh(self, tmp_path: Path) -> None:
        cfg = client_config(tmp_path, bindings=Bindings(), tracker="github-issues")
        target = select_target(cfg)
        assert target is not None
        assert target.cwd == _git_dir(cfg)

    def test_no_bindings_unresolved_tracker_targets_gh(self, tmp_path: Path) -> None:
        target = select_target(client_config(tmp_path, bindings=Bindings()))
        assert target is not None
        assert target.cwd == _git_dir(client_config(tmp_path, bindings=Bindings()))

    def test_no_bindings_non_github_is_none(self, tmp_path: Path) -> None:
        cfg = client_config(tmp_path, bindings=Bindings(), tracker="linear")
        assert select_target(cfg) is None

    def test_no_bindings_github_drain_delivers(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        entry = enqueue(ticket="42", parts=("hello",)).entry
        _drain(tmp_path, bindings=Bindings(), tracker="github-issues")
        (call,) = gh.comment_records()
        assert call.argv == ["gh", "issue", "comment", "42", "--body-file", "-"]
        assert _state(entry.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)

    def test_no_bindings_non_github_drain_releases_binding_unreachable(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        entry = enqueue(ticket="42", parts=("hello",)).entry
        _drain(tmp_path, bindings=Bindings(), tracker="linear")
        assert gh.records == []
        assert _state(entry.id) == (
            OutboxState.PENDING,
            OutboxReason.BINDING_UNREACHABLE,
        )

    def test_bindings_without_cli_stays_none(self, tmp_path: Path) -> None:
        # O2b: a codex MCP realization is a courier target; without one it stays None.
        cfg = client_config(
            tmp_path,
            bindings=bindings_linear_mcp(harnesses=("opencode",)),
            tracker="github-issues",
        )
        assert select_target(cfg) is None

    def test_bindings_cli_unchanged(self, tmp_path: Path) -> None:
        target = select_target(client_config(tmp_path))
        assert target is not None
        assert target.cwd == _git_dir(client_config(tmp_path))


class TestPartMarkers:
    def test_format(self) -> None:
        assert part_marker_line("m1", 1, 1) == "<!-- cw-outbox:m1:1/1 -->"
        assert part_marker_line("a.b:c", 2, 3) == "<!-- cw-outbox:a.b:c:2/3 -->"

    def test_multi_part_posts_in_order(self, tmp_path: Path, gh: GhThread) -> None:
        entry = enqueue(parts=("one", "two", "three")).entry

        _drain(tmp_path)

        stdins = [r.stdin for r in gh.comment_records()]
        assert stdins == [
            agent_marked_body(_posted_text(part, "m1", i, 3)).encode()
            for i, part in enumerate(("one", "two", "three"), start=1)
        ]
        assert _state(entry.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)

    def test_prefix_marker_does_not_false_match(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        gh.seed("T-1", "x\n\n<!-- cw-outbox:a:1:1/2 -->")
        entry = enqueue(marker="a", parts=("p",)).entry

        _drain(tmp_path)

        assert len(gh.comment_records()) == 1
        assert _state(entry.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)

    def test_marker_quoted_inside_prose_does_not_count(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        gh.seed("T-1", "see `<!-- cw-outbox:m1:1/1 -->` in the docs")
        entry = enqueue().entry

        _drain(tmp_path)

        assert len(gh.comment_records()) == 1
        assert _state(entry.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)


class TestDecisionTable:
    @pytest.mark.parametrize(
        ("role", "bindings", "tracker"),
        [
            (OutboxRole.KNOWLEDGE, None, None),
            (OutboxRole.TICKETS, Bindings(), "linear"),
            (OutboxRole.TICKETS, bindings_linear_mcp(harnesses=("opencode",)), None),
        ],
        ids=["knowledge-role", "no-binding", "mcp-without-codex"],
    )
    def test_g1_pending_released_binding_unreachable(
        self,
        tmp_path: Path,
        gh: GhThread,
        events: list[CapturedEvent],
        role: OutboxRole,
        bindings: Bindings | None,
        tracker: str | None,
    ) -> None:
        entry = enqueue(role=role).entry

        report = _drain(tmp_path, bindings=bindings, tracker=tracker)

        assert _state(entry.id) == (
            OutboxState.PENDING,
            OutboxReason.BINDING_UNREACHABLE,
        )
        assert gh.records == []
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_PENDING]
        assert events[0][1]["held"] is False
        assert marked_roles(CLIENT) == frozenset({role})
        assert read_held(CLIENT) == frozenset()
        assert report.unreachable_roles == [role.value]

    def test_g1_unverified_settles_unverified_with_marker(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        entry = _unverified()

        _drain(tmp_path, bindings=Bindings(), tracker="linear")

        assert _state(entry.id) == (
            OutboxState.UNVERIFIED,
            OutboxReason.READBACK_UNVERIFIED,
        )
        assert gh.records == []
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_UNVERIFIED]
        assert marked_roles(CLIENT) == frozenset({OutboxRole.TICKETS})

    def test_g2_pending_is_held_without_a_marker(
        self,
        tmp_path: Path,
        gh: GhThread,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = enqueue().entry
        monkeypatch.setattr("cw.outbox_drain.read_parts", _raise_record_error)

        report = _drain(tmp_path)

        assert _state(entry.id) == (OutboxState.PENDING, OutboxReason.TRANSIENT_FAILURE)
        assert gh.records == []
        assert read_held(CLIENT) == frozenset({entry.id})
        assert marked_roles(CLIENT) == frozenset()
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_PENDING]
        assert events[0][1]["held"] is True
        assert (report.released, report.held) == (1, 1)

    def test_g2_unverified_stays_unverified(
        self,
        tmp_path: Path,
        gh: GhThread,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = _unverified()
        monkeypatch.setattr("cw.outbox_drain.read_parts", _raise_record_error)

        _drain(tmp_path)

        assert _state(entry.id) == (
            OutboxState.UNVERIFIED,
            OutboxReason.READBACK_UNVERIFIED,
        )
        assert gh.records == []
        assert read_held(CLIENT) == frozenset()
        assert marked_roles(CLIENT) == frozenset()
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_UNVERIFIED]

    def test_l1_pending_released_with_marker(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        entry = enqueue().entry
        gh.unreadable("T-1")

        _drain(tmp_path)

        assert _state(entry.id) == (OutboxState.PENDING, OutboxReason.TRANSIENT_FAILURE)
        assert len(gh.records) == 1
        assert gh.comment_records() == []
        marker = read_marker(CLIENT, OutboxRole.TICKETS)
        assert marker is not None
        assert marker.reason is OutboxReason.TRANSIENT_FAILURE
        assert marker.announced == [entry.id]
        assert read_held(CLIENT) == frozenset()
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_PENDING]

    def test_l1_unverified_stays_unverified_with_marker(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        entry = _unverified()
        gh.unreadable("T-1")

        _drain(tmp_path)

        assert _state(entry.id) == (
            OutboxState.UNVERIFIED,
            OutboxReason.READBACK_UNVERIFIED,
        )
        assert gh.comment_records() == []
        assert marked_roles(CLIENT) == frozenset({OutboxRole.TICKETS})
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_UNVERIFIED]

    def test_l2_all_parts_present_is_marker_already_present(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        gh.seed("T-1", agent_marked_body(_posted_text("a", "m1", 1, 2)))
        gh.seed("T-1", agent_marked_body(_posted_text("b", "m1", 2, 2)))
        entry = enqueue(parts=("a", "b")).entry

        _drain(tmp_path)

        assert _state(entry.id) == (
            OutboxState.DELIVERED,
            OutboxReason.MARKER_ALREADY_PRESENT,
        )
        assert gh.comment_records() == []
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_DELIVERED]
        assert events[0][1]["posted"] == []

    def test_p1_posted(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        entry = enqueue(parts=("a", "b")).entry

        report = _drain(tmp_path)

        assert _state(entry.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)
        payload = events[0][1]
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_DELIVERED]
        assert [p["index"] for p in payload["posted"]] == [1, 2]
        assert all("#issuecomment-" in p["url"] for p in payload["posted"])
        assert report.delivered == 1

    def test_p2_refused_with_nothing_public_is_held(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        entry = enqueue().entry
        gh.script_comments("T-1", "refused")

        _drain(tmp_path)

        assert _state(entry.id) == (OutboxState.PENDING, OutboxReason.TRANSIENT_FAILURE)
        assert read_held(CLIENT) == frozenset({entry.id})
        assert marked_roles(CLIENT) == frozenset()
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_PENDING]
        assert events[0][1]["held"] is True

    def test_p3_refused_after_an_earlier_part_was_present(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        gh.seed("T-1", agent_marked_body(_posted_text("a", "m1", 1, 2)))
        entry = enqueue(parts=("a", "b")).entry
        gh.script_comments("T-1", "refused")

        _drain(tmp_path)

        assert _state(entry.id) == (
            OutboxState.UNVERIFIED,
            OutboxReason.READBACK_UNVERIFIED,
        )
        assert read_held(CLIENT) == frozenset()
        assert marked_roles(CLIENT) == frozenset()
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_UNVERIFIED]

    def test_p3_refused_after_an_earlier_part_posted_this_run(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        entry = enqueue(parts=("a", "b")).entry
        gh.script_comments("T-1", "ok", "refused")

        _drain(tmp_path)

        assert _state(entry.id) == (
            OutboxState.UNVERIFIED,
            OutboxReason.READBACK_UNVERIFIED,
        )
        assert read_held(CLIENT) == frozenset()
        assert [p["index"] for p in events[0][1]["posted"]] == [1]

    def test_p4_refused_from_unverified_stays_unverified(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        entry = _unverified()
        gh.script_comments("T-1", "refused")

        _drain(tmp_path)

        assert _state(entry.id) == (
            OutboxState.UNVERIFIED,
            OutboxReason.READBACK_UNVERIFIED,
        )
        assert read_held(CLIENT) == frozenset()
        assert marked_roles(CLIENT) == frozenset()
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_UNVERIFIED]

    def test_p5_outcome_unknown(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        entry = enqueue().entry
        gh.script_comments("T-1", "timeout")

        _drain(tmp_path)

        assert _state(entry.id) == (
            OutboxState.UNVERIFIED,
            OutboxReason.READBACK_UNVERIFIED,
        )
        assert marked_roles(CLIENT) == frozenset()
        assert read_held(CLIENT) == frozenset()
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_UNVERIFIED]

    @pytest.mark.parametrize("outcome", ["no_url", "missing"])
    def test_p6_url_or_readback_missing(
        self,
        tmp_path: Path,
        gh: GhThread,
        events: list[CapturedEvent],
        outcome: Any,
    ) -> None:
        entry = enqueue().entry
        gh.script_comments("T-1", outcome)

        _drain(tmp_path)

        assert _state(entry.id) == (
            OutboxState.UNVERIFIED,
            OutboxReason.READBACK_UNVERIFIED,
        )
        assert marked_roles(CLIENT) == frozenset()
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_UNVERIFIED]

    def test_p7_readback_mismatch(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        entry = enqueue().entry
        gh.script_comments("T-1", "mismatch")

        _drain(tmp_path)

        assert _state(entry.id) == (
            OutboxState.UNVERIFIED,
            OutboxReason.READBACK_MISMATCH,
        )
        assert marked_roles(CLIENT) == frozenset()
        assert events[0][1]["reason"] == "readback_mismatch"


class TestPerItemIsolation:
    @pytest.mark.parametrize("failing_first", [True, False])
    def test_one_refused_ticket_does_not_stop_another(
        self,
        tmp_path: Path,
        gh: GhThread,
        events: list[CapturedEvent],
        failing_first: bool,
    ) -> None:
        a = _at(0 if failing_first else 1, ticket="A", marker="a")
        b = _at(1 if failing_first else 0, ticket="B", marker="b")
        gh.script_comments("A", "refused")

        report = _drain(tmp_path)

        assert _state(a.id) == (OutboxState.PENDING, OutboxReason.TRANSIENT_FAILURE)
        assert read_held(CLIENT) == frozenset({a.id})
        assert _state(b.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)
        assert marked_roles(CLIENT) == frozenset()
        pending = [p for t, p, _c in events if t.value == "outbox.entry_pending"]
        assert len(pending) == 1
        assert pending[0]["held"] is True
        assert pending[0]["entry_id"] == a.id
        assert report.delivered == 1

    def test_timeout_then_next_entry_still_posts(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        a = _at(0, ticket="A", marker="a")
        b = _at(1, ticket="B", marker="b")
        gh.script_comments("A", "timeout")

        _drain(tmp_path)

        assert _state(a.id)[0] is OutboxState.UNVERIFIED
        assert _state(b.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)

    def test_unreadable_body_then_next_entry_still_posts(
        self, tmp_path: Path, gh: GhThread, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _at(0, ticket="A", marker="a")
        b = _at(1, ticket="B", marker="b")
        from cw import outbox_drain

        real = outbox_drain.read_parts

        def _flaky(entry: OutboxEntry) -> list[str]:
            if entry.id == a.id:
                return _raise_record_error(entry)
            return real(entry)

        monkeypatch.setattr("cw.outbox_drain.read_parts", _flaky)

        _drain(tmp_path)

        assert read_held(CLIENT) == frozenset({a.id})
        assert _state(b.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)

    def test_partial_then_next_entry_still_posts(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        a = _at(0, ticket="A", marker="a", parts=("1", "2"))
        b = _at(1, ticket="B", marker="b")
        gh.script_comments("A", "ok", "refused")

        _drain(tmp_path)

        assert _state(a.id)[0] is OutboxState.UNVERIFIED
        assert _state(b.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)

    def test_role_scoped_break(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        knowledge = _at(0, role=OutboxRole.KNOWLEDGE, marker="k")
        tickets = _at(1, marker="t")

        report = _drain(tmp_path)

        assert _state(knowledge.id) == (
            OutboxState.PENDING,
            OutboxReason.BINDING_UNREACHABLE,
        )
        assert _state(tickets.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)
        assert marked_roles(CLIENT) == frozenset({OutboxRole.KNOWLEDGE})
        assert report.unreachable_roles == ["knowledge"]


class TestHeld:
    def test_held_entry_not_reclaimed_in_the_same_run(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        entry = enqueue().entry
        gh.script_comments("T-1", "refused", "refused")

        report = _drain(tmp_path, limit=5)

        assert len(gh.comment_records()) == 1
        assert report.held == 1
        assert _state(entry.id)[0] is OutboxState.PENDING

    def test_second_non_force_run_skips_it_silently(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        enqueue()
        gh.script_comments("T-1", "refused")
        _drain(tmp_path)
        events.clear()
        calls = len(gh.records)

        report = _drain(tmp_path)

        assert len(gh.records) == calls
        assert events == []
        assert report == DrainReport(
            client=CLIENT,
            drained=0,
            delivered=0,
            unverified=0,
            skipped=0,
            released=0,
            held=0,
            lease_held=False,
            unreachable_roles=[],
        )

    def test_force_clears_held_and_markers_and_retries(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        entry = enqueue().entry
        knowledge = enqueue(role=OutboxRole.KNOWLEDGE, marker="k").entry
        gh.script_comments("T-1", "refused")
        _drain(tmp_path)
        assert read_held(CLIENT) == frozenset({entry.id})
        assert marked_roles(CLIENT) == frozenset({OutboxRole.KNOWLEDGE})

        report = _drain(tmp_path, force=True)

        assert _state(entry.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)
        assert read_held(CLIENT) == frozenset()
        # The knowledge entry is retried and re-marked in the new episode.
        assert _state(knowledge.id)[0] is OutboxState.PENDING
        assert report.delivered == 1

    def test_hold_survives_a_crash_later_in_the_run(
        self, tmp_path: Path, gh: GhThread, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _at(0, ticket="A", marker="a")
        b = _at(1, ticket="B", marker="b")
        gh.script_comments("A", "refused")
        from cw import outbox_drain

        real = outbox_drain.read_parts

        def _crash_on_b(entry: OutboxEntry) -> list[str]:
            if entry.id == b.id:
                msg = "crash"
                raise RuntimeError(msg)
            return real(entry)

        monkeypatch.setattr("cw.outbox_drain.read_parts", _crash_on_b)

        with pytest.raises(RuntimeError):
            _drain(tmp_path)

        assert read_held(CLIENT) == frozenset({a.id})
        assert _entry(b.id).state is OutboxState.CLAIMED


class TestRecheck:
    def test_rc1_all_present(self, tmp_path: Path, gh: GhThread) -> None:
        entry = _unverified()
        gh.seed("T-1", agent_marked_body(_posted_text("body", "m1", 1, 1)))

        _drain(tmp_path)

        assert _state(entry.id) == (
            OutboxState.DELIVERED,
            OutboxReason.MARKER_ALREADY_PRESENT,
        )
        assert gh.comment_records() == []

    def test_rc2_none_present_posts_all(self, tmp_path: Path, gh: GhThread) -> None:
        entry = _unverified(parts=("a", "b"))

        _drain(tmp_path)

        assert _state(entry.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)
        assert len(gh.comment_records()) == len(("a", "b"))

    def test_rc3_posts_only_the_missing_part(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        entry = _unverified(parts=("a", "b"))
        gh.seed("T-1", agent_marked_body(_posted_text("a", "m1", 1, 2)))

        _drain(tmp_path)

        (call,) = gh.comment_records()
        assert call.stdin is not None
        assert b":2/2 -->" in call.stdin
        assert _state(entry.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)

    def test_rc4_unreadable_listing_stays_unverified(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        entry = _unverified()
        gh.unreadable("T-1")

        _drain(tmp_path)

        assert _state(entry.id)[0] is OutboxState.UNVERIFIED
        assert gh.comment_records() == []

    def test_half_posted_end_to_end(self, tmp_path: Path, gh: GhThread) -> None:
        entry = enqueue(parts=("a", "b")).entry
        gh.script_comments("T-1", "ok", "refused")
        _drain(tmp_path)
        assert _state(entry.id)[0] is OutboxState.UNVERIFIED

        _drain(tmp_path)

        assert _state(entry.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)
        assert len(gh.comment_records()) == len(("1", "2", "2-retry"))
        assert b":2/2 -->" in (gh.comment_records()[-1].stdin or b"")


class TestOperatorAuthored:
    def test_no_agent_marker_but_the_outbox_marker(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        entry = enqueue(operator_authored=True, parts=("decided",)).entry

        _drain(tmp_path)

        (call,) = gh.comment_records()
        assert call.stdin == _posted_text("decided", "m1", 1, 1).encode()
        assert AGENT_COMMENT_MARKER.encode() not in (call.stdin or b"")
        assert _state(entry.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)

    def test_mismatch_is_readback_mismatch(self, tmp_path: Path, gh: GhThread) -> None:
        entry = enqueue(operator_authored=True).entry
        gh.script_comments("T-1", "mismatch")

        _drain(tmp_path)

        assert _state(entry.id) == (
            OutboxState.UNVERIFIED,
            OutboxReason.READBACK_MISMATCH,
        )

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            ("refused", (OutboxState.PENDING, OutboxReason.TRANSIENT_FAILURE)),
            ("timeout", (OutboxState.UNVERIFIED, OutboxReason.READBACK_UNVERIFIED)),
            ("no_url", (OutboxState.UNVERIFIED, OutboxReason.READBACK_UNVERIFIED)),
            ("missing", (OutboxState.UNVERIFIED, OutboxReason.READBACK_UNVERIFIED)),
        ],
    )
    def test_failures_follow_the_same_rows(
        self,
        tmp_path: Path,
        gh: GhThread,
        outcome: Any,
        expected: tuple[OutboxState, OutboxReason],
    ) -> None:
        entry = enqueue(operator_authored=True).entry
        gh.script_comments("T-1", outcome)

        _drain(tmp_path)

        assert _state(entry.id) == expected
        held = outcome == "refused"
        assert (entry.id in read_held(CLIENT)) is held


class TestRunLoop:
    def test_claims_one_at_a_time_up_to_the_cap(
        self, tmp_path: Path, gh: GhThread, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for index in range(5):
            _at(index, marker=f"m{index}")
        from cw import outbox_drain

        real = outbox_drain.claim
        limits: list[int] = []

        def _spy(client: str, **kwargs: Any) -> list[OutboxEntry]:
            limits.append(kwargs["limit"])
            return real(client, **kwargs)

        monkeypatch.setattr("cw.outbox_drain.claim", _spy)

        report = _drain(tmp_path, limit=2)

        assert set(limits) == {1}
        assert report.delivered == len(("m0", "m1"))
        assert len(gh.comment_records()) == len(("m0", "m1"))

    def test_configured_cap_applies_without_limit(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        for index in range(3):
            _at(index, marker=f"m{index}")
        cfg = client_config(tmp_path).model_copy(
            update={
                "outbox": client_config(tmp_path).outbox.model_copy(
                    update={"max_entries_per_run": 1}
                )
            }
        )

        report = run_drain(CLIENT, cfg, limit=None, force=False)

        assert report.delivered == 1

    def test_unverified_claimed_before_pending(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        pending = _at(0, ticket="P", marker="p")
        unverified = _unverified(1, ticket="U", marker="u")

        _drain(tmp_path)

        tickets = [r.argv[3] for r in gh.comment_records()]
        assert tickets == ["U", "P"]
        assert _state(pending.id)[0] is OutboxState.DELIVERED
        assert _state(unverified.id)[0] is OutboxState.DELIVERED

    def test_resettled_released_and_held_not_reclaimed(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        unverified = _unverified(0, ticket="U", marker="u")
        _at(1, ticket="P", marker="p")
        gh.script_comments("U", "timeout", "timeout")
        gh.script_comments("P", "refused", "refused")

        report = _drain(tmp_path, limit=10)

        assert len(gh.comment_records("U")) == 1
        assert len(gh.comment_records("P")) == 1
        assert (report.unverified, report.held) == (1, 1)
        assert _state(unverified.id)[0] is OutboxState.UNVERIFIED

    def test_announce_only_for_a_broken_role(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        first = _at(0, marker="a")
        second = _at(1, marker="b")

        report = _drain(tmp_path, bindings=Bindings(), tracker="linear")

        for entry in (first, second):
            assert _state(entry.id) == (
                OutboxState.PENDING,
                OutboxReason.BINDING_UNREACHABLE,
            )
        assert gh.records == []
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_PENDING] * 2
        assert report.released == len((first, second))
        marker = read_marker(CLIENT, OutboxRole.TICKETS)
        assert marker is not None
        assert marker.announced == [first.id, second.id]

    def test_unreadable_listing_breaks_the_role_for_later_tickets(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        first = _at(0, ticket="A", marker="a")
        second = _at(1, ticket="B", marker="b")
        gh.unreadable("A")

        report = _drain(tmp_path)

        for entry in (first, second):
            assert _state(entry.id) == (
                OutboxState.PENDING,
                OutboxReason.TRANSIENT_FAILURE,
            )
        assert [r.argv[3] for r in gh.records] == ["A"]
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_PENDING] * 2
        assert read_held(CLIENT) == frozenset()
        assert report.released == len((first, second))

    def test_announce_only_resettles_unverified_without_gh(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        first = _unverified(0, role=OutboxRole.KNOWLEDGE, marker="a")
        second = _unverified(1, role=OutboxRole.KNOWLEDGE, marker="b")
        pending = _at(2, role=OutboxRole.KNOWLEDGE, marker="c")

        report = _drain(tmp_path)

        assert gh.records == []
        for entry in (first, second):
            assert _state(entry.id)[0] is OutboxState.UNVERIFIED
        assert _state(pending.id) == (
            OutboxState.PENDING,
            OutboxReason.BINDING_UNREACHABLE,
        )
        assert _types(events) == [
            OrchestratorEventType.OUTBOX_ENTRY_UNVERIFIED,
            OrchestratorEventType.OUTBOX_ENTRY_UNVERIFIED,
            OrchestratorEventType.OUTBOX_ENTRY_PENDING,
        ]
        assert (report.unverified, report.released) == (2, 1)

    def test_announce_only_counts_toward_the_cap(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        _at(0, marker="a")
        second = _at(1, marker="b")

        report = _drain(tmp_path, bindings=Bindings(), tracker="linear", limit=1)

        assert report.released == 1
        assert _entry(second.id).reason is None

    def test_announce_once_per_episode_and_force_reannounces(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        enqueue()
        _drain(tmp_path, bindings=Bindings(), tracker="linear")
        assert len(events) == 1

        _drain(tmp_path, bindings=Bindings(), tracker="linear")
        assert len(events) == 1

        _drain(tmp_path, bindings=Bindings(), tracker="linear", force=True)
        assert len(events) == len(("first", "forced"))

    def test_successful_run_clears_its_role_marker(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        enqueue()
        _drain(tmp_path, bindings=Bindings(), tracker="linear")
        assert marked_roles(CLIENT) == frozenset({OutboxRole.TICKETS})

        report = _drain(tmp_path)

        assert marked_roles(CLIENT) == frozenset()
        assert report.unreachable_roles == []

    def test_binding_fault_run_does_not_clear(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        enqueue()
        gh.unreadable("T-1")
        _drain(tmp_path)

        _drain(tmp_path)

        assert marked_roles(CLIENT) == frozenset({OutboxRole.TICKETS})

    def test_claimant_is_the_current_launch_marker(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        entry = enqueue().entry

        _drain(tmp_path)

        claim_item = _entry(entry.id).transitions[-2]
        assert claim_item.initiator == "claim"
        assert claim_item.claimant == live_launch_marker()

    def test_none_claimant_is_never_recovered(
        self, tmp_path: Path, gh: GhThread, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry = enqueue().entry
        with monkeypatch.context() as patch:
            patch.setattr("cw.outbox._store.current_launch_marker", lambda: None)

            def _crash(_entry: OutboxEntry) -> list[str]:
                msg = "crash"
                raise RuntimeError(msg)

            patch.setattr("cw.outbox_drain.read_parts", _crash)
            with pytest.raises(RuntimeError):
                _drain(tmp_path)
        assert _entry(entry.id).claimant is None

        report = _drain(tmp_path)

        assert _entry(entry.id).state is OutboxState.CLAIMED
        assert report.drained == 0

    def test_store_settled_unreadable_body_is_skipped(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        entry = enqueue().entry
        _layout.body_path(CLIENT, entry.id).unlink()

        report = _drain(tmp_path)

        assert _state(entry.id) == (OutboxState.FAILED, OutboxReason.BODY_UNREADABLE)
        assert _types(events) == [OrchestratorEventType.OUTBOX_ENTRY_SKIPPED]
        assert events[0][1]["reason"] == "body_unreadable"
        assert (report.skipped, report.drained) == (1, 1)
        assert gh.records == []

    def test_dogfood_replay_dedups_on_the_marker(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        entry = enqueue(ticket="9", marker="dogfood").entry
        _drain(tmp_path)
        assert len(gh.comment_records()) == 1
        _layout.record_path(CLIENT, entry.id).unlink()

        again = enqueue(ticket="9", marker="dogfood")
        assert again.created is True
        _drain(tmp_path)

        assert _state(entry.id) == (
            OutboxState.DELIVERED,
            OutboxReason.MARKER_ALREADY_PRESENT,
        )
        assert len(gh.comment_records()) == 1


class TestDrainReport:
    def test_mixed_run(
        self,
        tmp_path: Path,
        gh: GhThread,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _at(0, ticket="D", marker="d")
        _at(1, ticket="U", marker="u")
        skipped = _at(2, ticket="S", marker="s")
        _at(3, ticket="H", marker="h")
        _at(4, ticket="K", marker="k", role=OutboxRole.KNOWLEDGE)
        _layout.body_path(CLIENT, skipped.id).unlink()
        gh.script_comments("U", "timeout")
        gh.script_comments("H", "refused")

        report = _drain(tmp_path)

        assert report == DrainReport(
            client=CLIENT,
            drained=3,
            delivered=1,
            unverified=1,
            skipped=1,
            released=2,
            held=1,
            lease_held=False,
            unreachable_roles=["knowledge"],
        )
        assert report.drained == report.delivered + report.unverified + report.skipped

    def test_lease_held_touches_nothing(self, tmp_path: Path, gh: GhThread) -> None:
        entry = enqueue().entry
        before = _layout.record_path(CLIENT, entry.id).read_bytes()

        with _hold_flock(_layout.drain_lock_path(CLIENT)):
            report = _drain(tmp_path)

        assert report.lease_held is True
        assert report.drained == report.released == report.delivered == 0
        assert gh.records == []
        assert _layout.record_path(CLIENT, entry.id).read_bytes() == before

    def test_cap_bounds_every_count_but_skipped(
        self, tmp_path: Path, gh: GhThread
    ) -> None:
        for index in range(3):
            entry = _at(index, marker=f"s{index}")
            _layout.body_path(CLIENT, entry.id).unlink()
        _at(3, marker="d0")
        _at(4, marker="d1")

        report = _drain(tmp_path, limit=1)

        assert report.delivered == 1
        assert report.skipped == len(("s0", "s1", "s2"))

    def test_report_model_is_frozen_and_closed(self) -> None:
        report = DrainReport(
            client=CLIENT,
            drained=0,
            delivered=0,
            unverified=0,
            skipped=0,
            released=0,
            held=0,
            lease_held=False,
            unreachable_roles=[],
        )
        with pytest.raises(ValueError, match="frozen"):
            report.drained = 1
        with pytest.raises(ValueError, match="extra"):
            DrainReport.model_validate({**report.model_dump(), "bogus": 1})


def _fail_with_record_error(*_args: object, **_kwargs: object) -> None:
    msg = "entry write failed"
    raise OutboxRecordError(msg)


class TestCrashConsistency:
    """Item 31: entry state first, then the held or marker record, then the event."""

    def test_binding_absent_release_failure_persists_no_marker_and_no_event(
        self,
        tmp_path: Path,
        gh: GhThread,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = enqueue().entry
        monkeypatch.setattr("cw.outbox_drain.release", _fail_with_record_error)

        with pytest.raises(OutboxRecordError):
            _drain(tmp_path, bindings=Bindings(), tracker="linear")

        assert marked_roles(CLIENT) == frozenset()
        assert read_marker(CLIENT, OutboxRole.TICKETS) is None
        assert events == []
        assert _state(entry.id)[0] is OutboxState.CLAIMED

    def test_unreadable_listing_release_failure_persists_no_marker(
        self,
        tmp_path: Path,
        gh: GhThread,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        enqueue()
        gh.unreadable("T-1")
        monkeypatch.setattr("cw.outbox_drain.release", _fail_with_record_error)

        with pytest.raises(OutboxRecordError):
            _drain(tmp_path)

        assert marked_roles(CLIENT) == frozenset()
        assert events == []

    def test_unverified_resettle_failure_persists_no_marker(
        self,
        tmp_path: Path,
        gh: GhThread,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _unverified(0, role=OutboxRole.KNOWLEDGE, marker="a")
        monkeypatch.setattr("cw.outbox_drain.settle", _fail_with_record_error)

        with pytest.raises(OutboxRecordError):
            _drain(tmp_path)

        assert marked_roles(CLIENT) == frozenset()
        assert events == []

    def test_committed_settle_failure_continues_to_next_entry(
        self,
        tmp_path: Path,
        gh: GhThread,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = _at(0, marker="first")
        second = _at(1, marker="second")
        real_settle = outbox_drain.settle
        raised = False

        def _commit_then_raise(*args: Any, **kwargs: Any) -> Any:
            nonlocal raised
            result = real_settle(*args, **kwargs)
            if not raised:
                raised = True
                msg = "reported after commit"
                raise OutboxRecordError(msg)
            return result

        monkeypatch.setattr("cw.outbox_drain.settle", _commit_then_raise)

        report = _drain(tmp_path)

        assert _state(first.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)
        assert _state(second.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)
        assert report.delivered == 2
        assert len(gh.comment_records()) == 2
        assert len(events) == 2

    def test_committed_claim_failure_continues_to_next_entry(
        self, tmp_path: Path, gh: GhThread, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = _at(0, marker="first")
        second = _at(1, marker="second")
        real_claim = outbox_drain.claim
        raised = False

        def _claim_then_raise(*args: Any, **kwargs: Any) -> list[OutboxEntry]:
            nonlocal raised
            result = real_claim(*args, **kwargs)
            if result and not raised:
                raised = True
                msg = "reported after claim commit"
                raise OutboxRecordError(msg)
            return result

        monkeypatch.setattr("cw.outbox_drain.claim", _claim_then_raise)

        with pytest.raises(OutboxRecordError):
            _drain(tmp_path)

        assert _entry(first.id).state is OutboxState.CLAIMED
        assert _state(second.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)
        assert len(gh.comment_records()) == 1

    def test_committed_unreadable_settlement_emits_skip_and_continues(
        self,
        tmp_path: Path,
        gh: GhThread,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        skipped = _at(0, marker="skipped")
        delivered = _at(1, marker="delivered")
        _layout.body_path(CLIENT, skipped.id).unlink()
        real_write = _layout.write_record

        def _write_then_raise(client: str, entry: OutboxEntry) -> None:
            real_write(client, entry)
            if entry.state is OutboxState.FAILED:
                msg = "reported after unreadable settlement commit"
                raise OutboxRecordError(msg)

        monkeypatch.setattr("cw.outbox._layout.write_record", _write_then_raise)

        report = _drain(tmp_path)

        assert _state(skipped.id) == (
            OutboxState.FAILED,
            OutboxReason.BODY_UNREADABLE,
        )
        assert _state(delivered.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)
        assert report.skipped == 1
        assert report.delivered == 1
        assert _types(events) == [
            OrchestratorEventType.OUTBOX_ENTRY_DELIVERED,
            OrchestratorEventType.OUTBOX_ENTRY_SKIPPED,
        ]

    def test_marker_write_failure_does_not_hold_pending_and_fails_the_run(
        self,
        tmp_path: Path,
        gh: GhThread,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = enqueue().entry

        def _fail_marker(*_args: Any, **_kwargs: Any) -> None:
            msg = "marker sidecar is unwritable"
            raise OutboxRecordError(msg)

        monkeypatch.setattr("cw.outbox_drain.write_marker", _fail_marker)

        with pytest.raises(OutboxRecordError):
            _drain(tmp_path, bindings=Bindings(), tracker="linear")

        assert _state(entry.id) == (
            OutboxState.PENDING,
            OutboxReason.BINDING_UNREACHABLE,
        )
        assert read_held(CLIENT) == frozenset()
        assert events[0][0] is OrchestratorEventType.OUTBOX_ENTRY_PENDING
        assert events[0][1]["held"] is False

    def test_held_list_write_failure_surfaces_committed_release(
        self,
        tmp_path: Path,
        gh: GhThread,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = enqueue().entry
        monkeypatch.setattr("cw.outbox_drain.read_parts", _raise_record_error)

        def _fail_held(*_args: Any, **_kwargs: Any) -> None:
            msg = "held sidecar is unwritable"
            raise OutboxRecordError(msg)

        monkeypatch.setattr("cw.outbox_drain.append_held", _fail_held)

        with pytest.raises(OutboxRecordError):
            _drain(tmp_path)

        assert _state(entry.id) == (
            OutboxState.PENDING,
            OutboxReason.TRANSIENT_FAILURE,
        )
        assert events[0][0] is OrchestratorEventType.OUTBOX_ENTRY_PENDING
        assert events[0][1]["held"] is False

    @pytest.mark.parametrize("unverified", [False, True])
    def test_marker_is_written_after_the_entry_and_before_the_event(
        self,
        tmp_path: Path,
        gh: GhThread,
        monkeypatch: pytest.MonkeyPatch,
        unverified: bool,
    ) -> None:
        if unverified:
            _unverified(0, role=OutboxRole.KNOWLEDGE, marker="a")
        else:
            enqueue()
        order: list[str] = []
        for name in ("release", "settle", "write_marker", "record_event"):
            real = getattr(outbox_drain, name)

            def _spy(
                *args: Any, _real: Any = real, _name: str = name, **kw: Any
            ) -> Any:
                order.append(_name)
                return _real(*args, **kw)

            monkeypatch.setattr(f"cw.outbox_drain.{name}", _spy)

        _drain(tmp_path, bindings=None if unverified else Bindings(), tracker="linear")

        entry_write = "settle" if unverified else "release"
        assert order == [entry_write, "write_marker", "record_event"]

    def test_held_entry_is_released_before_the_held_record(
        self,
        tmp_path: Path,
        gh: GhThread,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        enqueue()
        monkeypatch.setattr("cw.outbox_drain.read_parts", _raise_record_error)
        order: list[str] = []
        for name in ("release", "append_held", "record_event"):
            real = getattr(outbox_drain, name)

            def _spy(
                *args: Any, _real: Any = real, _name: str = name, **kw: Any
            ) -> Any:
                order.append(_name)
                return _real(*args, **kw)

            monkeypatch.setattr(f"cw.outbox_drain.{name}", _spy)

        _drain(tmp_path)

        assert order == ["release", "append_held", "record_event"]


class TestEvents:
    def test_payload_keys_and_no_body(
        self, tmp_path: Path, gh: GhThread, events: list[CapturedEvent]
    ) -> None:
        entry = enqueue(ticket="77", parts=("secret prose",)).entry

        _drain(tmp_path)

        ((etype, payload, correlation_id),) = events
        assert etype is OrchestratorEventType.OUTBOX_ENTRY_DELIVERED
        assert set(payload) == _PAYLOAD_KEYS
        assert payload["entry_id"] == entry.id
        assert payload["client"] == CLIENT
        assert payload["role"] == "tickets"
        assert payload["producer"] == "codex-review"
        assert payload["held"] is False
        assert "secret prose" not in repr(payload)
        assert correlation_id == "77"

    def test_emit_failure_is_swallowed_with_one_warning(
        self,
        tmp_path: Path,
        gh: GhThread,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        entry = enqueue().entry

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "events dir unwritable"
            raise CwError(msg)

        monkeypatch.setattr("cw.outbox_drain.record_event", _boom)

        with caplog.at_level(logging.WARNING, logger="cw.outbox_drain"):
            _drain(tmp_path)

        assert _state(entry.id) == (OutboxState.DELIVERED, OutboxReason.POSTED)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_values(self) -> None:
        assert {
            OrchestratorEventType.OUTBOX_ENTRY_DELIVERED.value,
            OrchestratorEventType.OUTBOX_ENTRY_UNVERIFIED.value,
            OrchestratorEventType.OUTBOX_ENTRY_SKIPPED.value,
            OrchestratorEventType.OUTBOX_ENTRY_PENDING.value,
        } == {
            "outbox.entry_delivered",
            "outbox.entry_unverified",
            "outbox.entry_skipped",
            "outbox.entry_pending",
        }


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


class TestLayering:
    def test_engine_imports_no_tracker_io_codex_review_or_subprocess(self) -> None:
        modules = _imported_modules(_ENGINE)

        assert "subprocess" not in modules
        # O2b: the courier boundary is the only tracker-IO surface the engine
        # may import: its turn loop and the part dataclass (no turn code).
        tracker_io = {m for m in modules if m.startswith("cw.tracker_io")}
        assert tracker_io <= {"cw.tracker_io._drain", "cw.tracker_io._dedup"}
        assert not any(m.startswith("cw.codex_review") for m in modules)


class TestNoGhOutsideAllowlist:
    def test_engine_has_no_gh_argv_literal(self) -> None:
        tree = ast.parse(_ENGINE.read_text(encoding="utf-8"))
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

        assert "gh" not in literals
