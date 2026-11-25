"""Tests for cw.codex_plan._tracker: the resume round's tracker read (#3464)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from cw.codex_fix_loop.constraints import RESOLUTIONS_MARKER
from cw.codex_plan._tracker import (
    SOURCE_NONE,
    TrackerObservation,
    body_resolutions_section,
    body_source,
    discover_resolutions_source,
    observe_tracker,
    source_observed,
)
from cw.context_queue_metadata import operator_resolutions_source
from cw.gh import AGENT_COMMENT_MARKER
from cw.models.operator_resolution import OperatorResolution
from cw.plan_fingerprint import compute_body_sha
from cw.ticket_snapshot import SnapshotContext
from tests._shared.ticket_context import make_comment, make_snapshot_from

_GITHUB = SnapshotContext(github=True)
_MCP = SnapshotContext(github=False, operator_ids=("operator",))
_MARKED = f"Resolutions\n{RESOLUTIONS_MARKER}\nR1: do X"


def _resolution(round_: int = 1, text: str = "do X") -> OperatorResolution:
    return OperatorResolution(
        text=text,
        recorded_at=datetime(2026, 11, 20, tzinfo=UTC),
        actor="operator",
        round=round_,
    )


class TestObserve:
    def test_operator_comment_ignores_pipeline_and_agent_comments(self) -> None:
        snapshot = make_snapshot_from(
            [
                make_comment("human", body="please do X"),
                make_comment(
                    "marked", body=f"hi\n{AGENT_COMMENT_MARKER}", agent_authored=True
                ),
                make_comment("fixed", body="## Pending Verification Scan\n\nA1"),
                make_comment("por", body="<!-- plan-spec-reviewed: x -->\nplan"),
                make_comment("selfdisc", body="x", agent_authored=True),
            ]
        )
        observation = observe_tracker(snapshot, _GITHUB)
        assert observation.observed
        assert observation.operator_comment == "human"

    def test_operator_comment_is_none_when_no_comment_survives(self) -> None:
        snapshot = make_snapshot_from([make_comment("a", agent_authored=True)])
        observation = observe_tracker(snapshot, _GITHUB)
        assert observation.operator_comment == SOURCE_NONE
        assert observation.fingerprint == (
            f"operator_comment=none|body_sha={compute_body_sha(snapshot.body)}"
        )

    def test_a_minimized_operator_comment_still_counts(self) -> None:
        snapshot = make_snapshot_from([make_comment("m", minimized=True)])
        assert observe_tracker(snapshot, _GITHUB).operator_comment == "m"

    def test_non_github_rules_drop_foreign_and_conflicted_authors(self) -> None:
        snapshot = make_snapshot_from(
            [
                make_comment("ok"),
                make_comment("foreign", author_id="someone"),
                make_comment("conflict", author_conflict=True),
            ]
        )
        observation = observe_tracker(snapshot, _MCP)
        assert [c["id"] for c in observation.comments] == ["ok"]

    def test_empty_operator_ids_observe_nothing(self) -> None:
        snapshot = make_snapshot_from([make_comment("ok")])
        observation = observe_tracker(snapshot, SnapshotContext(github=False))
        assert observation == TrackerObservation(observed=False)
        assert observation.fingerprint is None

    def test_body_sha_is_the_cw_body_sha_function(self) -> None:
        snapshot = make_snapshot_from([], body="the body\n\n")
        observation = observe_tracker(snapshot, _GITHUB)
        assert observation.body_sha == compute_body_sha("the body\n\n")

    def test_an_absent_snapshot_is_unobserved(self) -> None:
        assert observe_tracker(None, _GITHUB) == TrackerObservation(observed=False)


class TestDiscover:
    def test_row_first_with_a_mirror_friction(self) -> None:
        snapshot = make_snapshot_from([make_comment("c7", body=_MARKED)])
        resolutions = [_resolution(1), _resolution(2, "do Y")]
        found = discover_resolutions_source(
            resolutions, observe_tracker(snapshot, _GITHUB)
        )
        assert found.current == operator_resolutions_source(resolutions)
        assert found.friction == (
            "resolutions-comment-ignored-as-mirror: comment c7 carries the preflight "
            "marker but the dev-queue row already holds operator_resolutions "
            "(round 2); record it with cw dev-queue resolve",
        )

    def test_the_row_channel_works_unobserved(self) -> None:
        resolutions = [_resolution()]
        found = discover_resolutions_source(
            resolutions, TrackerObservation(observed=False)
        )
        assert found.current == operator_resolutions_source(resolutions)
        assert found.friction == ()

    def test_multi_marker_newest_is_last_in_snapshot_order(self) -> None:
        snapshot = make_snapshot_from(
            [
                make_comment("old", body=_MARKED, created_at="2026-11-25T00:00:00Z"),
                make_comment("new", body=_MARKED, created_at="2026-11-01T00:00:00Z"),
            ]
        )
        found = discover_resolutions_source([], observe_tracker(snapshot, _GITHUB))
        assert found.current == "comment:new"
        (line,) = found.friction
        assert line.startswith("multi-marker: 2 resolution comments found")
        assert "newest (last in snapshot order) used" in line

    def test_one_marker_comment(self) -> None:
        snapshot = make_snapshot_from([make_comment("c1", body=_MARKED)])
        found = discover_resolutions_source([], observe_tracker(snapshot, _GITHUB))
        assert found.current == "comment:c1"
        assert found.friction == ()

    def test_body_over_comment_and_an_echo_does_not_trip(self) -> None:
        body = f"# Ticket\n\nText\n\n## Resolutions\nR1: X\n{RESOLUTIONS_MARKER}\n"
        snapshot = make_snapshot_from(
            [make_comment("a", body=_MARKED), make_comment("b", body=_MARKED)],
            body=body,
        )
        found = discover_resolutions_source([], observe_tracker(snapshot, _GITHUB))
        assert found.current == body_source(body)
        assert found.friction == ()

    def test_no_marker_is_none(self) -> None:
        snapshot = make_snapshot_from([make_comment("a")])
        found = discover_resolutions_source([], observe_tracker(snapshot, _GITHUB))
        assert found.current == SOURCE_NONE

    @pytest.mark.parametrize(
        "ctx", [_GITHUB, SnapshotContext(github=False)], ids=["absent", "unreadable"]
    )
    def test_unobserved_without_a_row_is_undeterminable(
        self, ctx: SnapshotContext
    ) -> None:
        snapshot = None if ctx.github else make_snapshot_from([])
        found = discover_resolutions_source([], observe_tracker(snapshot, ctx))
        assert found.current is None


class TestBodySection:
    def test_from_the_last_heading_through_the_marker(self) -> None:
        body = f"# T\n\nintro\n\n## Res\n\nR1: X\n{RESOLUTIONS_MARKER}\ntrailing\n"
        assert (
            body_resolutions_section(body) == f"## Res\n\nR1: X\n{RESOLUTIONS_MARKER}"
        )
        digest = hashlib.sha256(
            f"## Res\n\nR1: X\n{RESOLUTIONS_MARKER}".encode()
        ).hexdigest()
        assert body_source(body) == f"body:{digest}"

    def test_without_a_heading_from_the_start(self) -> None:
        body = f"\nR1: X\n{RESOLUTIONS_MARKER}\n"
        assert body_resolutions_section(body) == f"R1: X\n{RESOLUTIONS_MARKER}"

    def test_without_the_marker(self) -> None:
        assert body_resolutions_section("# T\n") is None
        assert body_source("# T\n") is None


class TestSourceObserved:
    def test_comment_source(self) -> None:
        snapshot = make_snapshot_from(
            [make_comment("A", body=_MARKED), make_comment("x", body="plain")]
        )
        observation = observe_tracker(snapshot, _GITHUB)
        assert source_observed("comment:A", observation)
        assert not source_observed("comment:x", observation)
        assert not source_observed("comment:gone", observation)

    def test_body_source(self) -> None:
        body = f"## R\n{RESOLUTIONS_MARKER}\n"
        observed = observe_tracker(make_snapshot_from([], body=body), _GITHUB)
        gone = observe_tracker(make_snapshot_from([], body="# T\n"), _GITHUB)
        assert source_observed("body:" + "0" * 64, observed)
        assert not source_observed("body:" + "0" * 64, gone)

    def test_other_sources_and_unobserved(self) -> None:
        observation = observe_tracker(make_snapshot_from([]), _GITHUB)
        assert not source_observed("row:1", observation)
        assert not source_observed("comment:A", TrackerObservation(observed=False))
