"""CLI end-to-end: ``cw dev-queue approve --post-marker`` queues on the outbox.

The plan-approved audit marker is no longer posted inline (RFC 0019 O3b, #3754):
``approve`` enqueues it through :func:`cw.outbox_producer.enqueue_prose` and the
outbox drain delivers it on any tracker. These tests assert the stored entry
(producer, marker, ``operator_authored``, ``redacted``, parts), the exact echoes
and that no ``gh`` process is launched. They are hermetic: the delivery side
lives in ``tests/test_outbox_drain.py``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from click import Option

from cw.cli.dev_queue._plan_marker import (
    _PLAN_APPROVED_MARKER,
    _plan_approved_marker,
    _plan_approved_outbox_marker,
)
from cw.cli.dev_queue.approve import _bound_fingerprint, dev_queue_approve
from cw.models import PLAN_APPROVED_FINGERPRINT_KEY, Stage
from cw.outbox import MAX_MARKER_CHARS
from cw.outbox_producer import EnqueueOutcome
from tests._cli_review_helpers import _enter_operator_machine
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._shared.cli_preflight import _write_tracker_config
from tests._shared.outbox_builders import enqueue, entries_for
from tests._shared.review_builders import stub_fetch_plan
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    PLAN_PENDING,
    REVIEW_PENDING,
    bound_session_for,
    invoke_dev_queue,
    make_blocked,
    patch_roster,
    queue_row,
    seed_queue,
    write_lane_client,
)
from tests._tool_fakes import FakeGhCli

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

_FP = "a" * 64
_PRODUCER = "approve-post-marker"
_HINT = (
    "Pass --post-marker to also queue the plan-approved audit"
    " marker comment on this ticket."
)


@pytest.fixture(autouse=True)
def _operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGhCli:
    write_lane_client(ACME, tmp_path / "ws")
    patch_roster(monkeypatch, set())
    _enter_operator_machine(monkeypatch, tmp_path)
    # approve_ticket's plan-reviewed check reads the plan-of-record comment;
    # None takes the "re-queue at plan stage" branch without launching gh.
    stub_fetch_plan(
        monkeypatch, None, target="cw.dev_queue.lifecycle.fetch_approved_plan_comment"
    )
    return FakeGhCli().install(monkeypatch)


@pytest.fixture
def gh(_operator: FakeGhCli) -> FakeGhCli:
    return _operator


def _seed_plan(fingerprint: str | None = None) -> None:
    last_result: dict[str, object] = dict(PLAN_PENDING)
    if fingerprint is not None:
        last_result["plan_draft_fingerprint"] = fingerprint
    task = make_blocked(stage=Stage.PLAN)
    seed_queue([task], [bound_session_for(task, last_result=last_result)])


def _approve(*extra: str) -> Result:
    return invoke_dev_queue("approve", ACME_TICKET, "-c", ACME, *extra)


def _marker_entries() -> list[tuple[str, str, bool, bool, list[str]]]:
    """``(marker, producer, operator_authored, redacted, parts)`` per entry."""
    return [
        (
            produced.entry.marker,
            produced.entry.producer,
            produced.entry.operator_authored,
            produced.entry.redacted,
            produced.parts,
        )
        for produced in entries_for(ACME, ticket=ACME_TICKET, producer=_PRODUCER)
    ]


def _queued_line(entry_id: str, draft: str = "") -> str:
    return (
        f"--post-marker: queued the plan-approved marker comment for"
        f" {ACME_TICKET} ({ACME}){draft} (outbox entry {entry_id}).\n"
    )


class TestPostMarkerEnqueues:
    def test_post_marker_enqueues_bare_marker_on_plan_stage(
        self, gh: FakeGhCli
    ) -> None:
        _seed_plan()
        result = _approve("--post-marker")
        assert result.exit_code == 0, result.output
        assert _marker_entries() == [
            (
                "plan-approved-bare",
                _PRODUCER,
                True,
                False,
                [_plan_approved_marker(None)],
            )
        ]
        (produced,) = entries_for(ACME, ticket=ACME_TICKET)
        assert _queued_line(produced.entry.id) in result.stdout
        assert "queued" not in result.stderr
        assert gh.records == []

    def test_approve_no_longer_imports_the_inline_comment_poster(self) -> None:
        from cw.cli.dev_queue import approve

        assert not hasattr(approve, "post_issue_comment")
        assert not hasattr(approve, "fetch_issue_comments")

    def test_post_marker_embeds_fingerprint_when_bound(self) -> None:
        _seed_plan(_FP)
        result = _approve("--post-marker")
        assert result.exit_code == 0, result.output
        assert _marker_entries() == [
            (
                f"plan-approved-{_FP}",
                _PRODUCER,
                True,
                False,
                [f"<!-- auto-dev-plan-approved: {_FP} -->"],
            )
        ]
        (produced,) = entries_for(ACME, ticket=ACME_TICKET)
        assert _queued_line(produced.entry.id, f" for draft {_FP[:12]}") in (
            result.stdout
        )

    def test_post_marker_queues_on_linear_tracker(
        self, tmp_path: Path, gh: FakeGhCli
    ) -> None:
        _write_tracker_config(tmp_path / "ws", "linear")
        _seed_plan()
        result = _approve("--post-marker")
        assert result.exit_code == 0, result.output
        (marker, producer, *_rest) = _marker_entries()[0]
        assert (marker, producer) == ("plan-approved-bare", _PRODUCER)
        assert "not GitHub" not in result.output
        assert "queued the plan-approved marker comment" in result.stdout
        assert gh.records == []

    def test_post_marker_outbox_disabled_still_enqueues(self, tmp_path: Path) -> None:
        write_clients_yaml(
            ClientSpec(
                ACME,
                tmp_path / "ws",
                lanes=[{"name": "default"}],
                extra={"outbox": {"enabled": False}},
            ),
            ensure_workspaces=True,
        )
        _seed_plan()
        result = _approve("--post-marker")
        assert result.exit_code == 0, result.output
        assert [e[0] for e in _marker_entries()] == ["plan-approved-bare"]
        assert "queued the plan-approved marker comment" in result.stdout

    def test_post_marker_warns_on_review_stage(self, gh: FakeGhCli) -> None:
        task = make_blocked(stage=Stage.REVIEW)
        seed_queue([task], [bound_session_for(task, last_result=REVIEW_PENDING)])
        result = _approve("--post-marker")
        assert result.exit_code == 0, result.output
        assert (
            "--post-marker is PLAN-stage-only; ticket is at 'review' — marker not"
            " posted.\n"
        ) in result.stderr
        assert entries_for(ACME) == []
        assert gh.records == []


class TestPostMarkerDedup:
    def test_duplicate_echoes_entry_and_state(self) -> None:
        _seed_plan()
        first = enqueue(
            ACME,
            ticket=ACME_TICKET,
            marker="plan-approved-bare",
            producer=_PRODUCER,
            parts=[_plan_approved_marker(None)],
            operator_authored=True,
        )
        result = _approve("--post-marker")
        assert result.exit_code == 0, result.output
        assert len(entries_for(ACME, ticket=ACME_TICKET)) == 1
        assert (
            f"--post-marker: the plan-approved marker comment for {ACME_TICKET}"
            f" ({ACME}) is already queued (entry {first.entry.id}, state pending)"
            " — no duplicate queued.\n"
        ) in result.stdout
        assert "queued the plan-approved marker comment" not in result.stdout
        assert _HINT not in result.output

    def test_duplicate_same_fingerprint_names_the_draft(self) -> None:
        _seed_plan(_FP)
        first = enqueue(
            ACME,
            ticket=ACME_TICKET,
            marker=f"plan-approved-{_FP}",
            producer=_PRODUCER,
            parts=[_plan_approved_marker(_FP)],
            operator_authored=True,
        )
        result = _approve("--post-marker")
        assert result.exit_code == 0, result.output
        assert len(entries_for(ACME, ticket=ACME_TICKET)) == 1
        assert (
            f"({ACME}) for draft {_FP[:12]} is already queued (entry"
            f" {first.entry.id}, state pending)"
        ) in result.stdout
        assert _HINT not in result.output

    def test_changed_fingerprint_queues_second_entry(self) -> None:
        old_fp = "a" * 63 + "b"
        _seed_plan(_FP)
        enqueue(
            ACME,
            ticket=ACME_TICKET,
            marker=f"plan-approved-{old_fp}",
            producer=_PRODUCER,
            parts=[_plan_approved_marker(old_fp)],
            operator_authored=True,
        )
        result = _approve("--post-marker")
        assert result.exit_code == 0, result.output
        assert [e[0] for e in _marker_entries()] == [
            f"plan-approved-{old_fp}",
            f"plan-approved-{_FP}",
        ]
        assert "queued the plan-approved marker comment" in result.stdout
        assert "already queued" not in result.stdout

    def test_bare_entry_does_not_dedup_bound_approval(self) -> None:
        """A bare entry attributes to no draft, so a bound approval still queues."""
        _seed_plan(_FP)
        enqueue(
            ACME,
            ticket=ACME_TICKET,
            marker="plan-approved-bare",
            producer=_PRODUCER,
            parts=[_plan_approved_marker(None)],
            operator_authored=True,
        )
        result = _approve("--post-marker")
        assert result.exit_code == 0, result.output
        assert [e[0] for e in _marker_entries()] == [
            "plan-approved-bare",
            f"plan-approved-{_FP}",
        ]

    def test_no_fingerprint_ignores_bound_entry_and_queues_bare(self) -> None:
        _seed_plan()
        enqueue(
            ACME,
            ticket=ACME_TICKET,
            marker=f"plan-approved-{_FP}",
            producer=_PRODUCER,
            parts=[_plan_approved_marker(_FP)],
            operator_authored=True,
        )
        result = _approve("--post-marker")
        assert result.exit_code == 0, result.output
        assert [e[0] for e in _marker_entries()] == [
            f"plan-approved-{_FP}",
            "plan-approved-bare",
        ]

    def test_malformed_fingerprint_falls_back_to_bare_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A malformed fingerprint never reaches the entry; the warning reports
        its length, never its value (it could carry ``-->``)."""
        malformed = "x --> <!-- evil"
        _seed_plan(malformed)
        with caplog.at_level(logging.WARNING, logger="cw.dev_queue.approval"):
            result = _approve("--post-marker")
        assert result.exit_code == 0, result.output
        assert _marker_entries() == [
            (
                "plan-approved-bare",
                _PRODUCER,
                True,
                False,
                [_PLAN_APPROVED_MARKER],
            )
        ]
        assert "not a 64-character lowercase hex" in caplog.text
        assert f"got {len(malformed)} characters" in caplog.text
        assert "evil" not in result.output
        assert "evil" not in caplog.text
        assert "-->" not in caplog.text


class TestPostMarkerFailure:
    def test_enqueue_failure_warns_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_plan(_FP)
        calls: list[tuple[object, ...]] = []

        def _fail(*args: object, **_kw: object) -> EnqueueOutcome:
            calls.append(args)
            return EnqueueOutcome(None, False, "outbox_enqueue_failed")

        monkeypatch.setattr("cw.cli.dev_queue.approve.enqueue_prose", _fail)
        result = _approve("--post-marker")
        assert result.exit_code == 0, result.output
        assert [call[:4] for call in calls] == [
            (ACME, ACME_TICKET, _PRODUCER, f"plan-approved-{_FP}")
        ]
        assert (
            f"--post-marker: could not queue the plan-approved marker comment for"
            f" {ACME_TICKET} ({ACME}) for draft {_FP[:12]} (outbox_enqueue_failed);"
            " the approval is recorded on the row regardless.\n"
        ) in result.stderr
        assert "queued the plan-approved marker" not in result.stdout
        # The approval itself landed, and the tip still points at the flag.
        assert queue_row().plan_approved_at is not None
        assert _HINT in result.stdout
        assert entries_for(ACME) == []


class TestRequeueHint:
    def test_requeue_message_mentions_post_marker(self) -> None:
        _seed_plan()
        result = _approve()
        assert result.exit_code == 0, result.output
        assert (
            f"Approved {ACME_TICKET} ({ACME}): plan not yet quality-reviewed"
            " — re-queued at plan stage to run Plan Quality Review."
            " Re-run auto-dev-plan (or dispatch) to proceed."
        ) in result.output
        assert _HINT in result.output
        assert entries_for(ACME) == []

    def test_hint_suppressed_when_marker_just_queued(self) -> None:
        _seed_plan()
        result = _approve("--post-marker")
        assert result.exit_code == 0, result.output
        assert "plan not yet quality-reviewed" in result.output
        assert _HINT not in result.output

    def test_hint_shown_on_github_tracker(self) -> None:
        _seed_plan()
        result = _approve()
        assert result.exit_code == 0, result.output
        assert "Pass --post-marker" in result.output

    def test_hint_shown_on_linear_tracker(self, tmp_path: Path) -> None:
        _write_tracker_config(tmp_path / "ws", "linear")
        _seed_plan()
        result = _approve()
        assert result.exit_code == 0, result.output
        assert "re-queued at plan stage" in result.output
        assert (
            "recorded on the dev-queue row (plan_approved_at +"
            " plan_approved_fingerprint)" in result.output
        )
        assert _HINT in result.output
        assert entries_for(ACME) == []


class TestMarkerHelpers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("x --> <!-- evil", None, id="malformed-string"),
            pytest.param("a" * 62, None, id="truncated"),
            pytest.param(None, None, id="none"),
            pytest.param(True, None, id="non-string"),
            pytest.param("b" * 64, "b" * 64, id="well-formed"),
        ],
    )
    def test_bound_fingerprint_guard_holds_independently_of_the_stamp(
        self, raw: str | bool | None, expected: str | None
    ) -> None:
        """#2382 moved the malformed-value rejection upstream into the stamp,
        so this boundary guard is no longer reached by a malformed value
        end-to-end. It still has to hold on its own."""
        assert _bound_fingerprint({PLAN_APPROVED_FINGERPRINT_KEY: raw}) == expected

    @pytest.mark.parametrize(
        ("fingerprint", "expected"),
        [
            pytest.param(_FP, f"plan-approved-{_FP}", id="bound"),
            pytest.param("0" * 63 + "f", f"plan-approved-{'0' * 63}f", id="bound-hex"),
            pytest.param(None, "plan-approved-bare", id="bare"),
            pytest.param("", "plan-approved-bare", id="empty"),
            pytest.param("a" * 63, "plan-approved-bare", id="short"),
            pytest.param("A" * 64, "plan-approved-bare", id="uppercase"),
            pytest.param("a" * 64 + "\n", "plan-approved-bare", id="newline"),
            pytest.param("x --> <!-- evil", "plan-approved-bare", id="malformed"),
        ],
    )
    def test_plan_approved_outbox_marker_shapes(
        self, fingerprint: str | None, expected: str
    ) -> None:
        marker = _plan_approved_outbox_marker(fingerprint)
        assert marker == expected
        assert len(marker) <= MAX_MARKER_CHARS
        assert "<" not in marker
        assert " " not in marker

    def test_bound_and_bare_outbox_markers_differ(self) -> None:
        assert _plan_approved_outbox_marker(_FP) != _plan_approved_outbox_marker(None)


class TestPostMarkerHelp:
    def test_post_marker_help_states_audit_only_and_the_outbox(self) -> None:
        """Read off the option object, since ``--help`` rewraps hyphenated words."""
        option = next(
            param
            for param in dev_queue_approve.params
            if isinstance(param, Option) and param.name == "post_marker"
        )
        assert option.help is not None
        assert "audit-only" in option.help
        assert "outbox" in option.help
