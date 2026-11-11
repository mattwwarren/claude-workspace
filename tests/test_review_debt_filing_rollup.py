"""Tests for the review-debt roll-up issue (``_rollup`` and its filing flow)."""

from __future__ import annotations

import pytest

from cw.gh import GhFailure
from cw.models import OrchestratorEventType
from cw.models.review_debt_record import ReviewDebtRecord
from cw.review_debt_filing import DebtEntry, FilingFailureKind, file_review_debt
from cw.review_debt_filing._constants import (
    ROLLUP_BODY_MAX_CHARS,
    ROLLUP_EVIDENCE_MAX_CHARS,
    ROLLUP_FIELD_MAX_CHARS,
)
from cw.review_debt_filing._marker import (
    debt_marker,
    parse_pr_markers,
    pr_marker,
    queue_ticket_ref,
    scope_prefix,
)
from cw.review_debt_filing._render import render_body as render_legacy_body
from cw.review_debt_filing._rollup import (
    RollupGroup,
    append_items,
    group_title,
    is_rollup,
    plan_groups,
    render_body,
    render_item,
    rollup_marker,
    rollup_part,
    scoped_rollup,
)
from tests._review_debt_helpers import (
    FakeClock,
    _clock_stepper,
    _debt_ctx,
    _debt_entry,
    _debt_record,
    _events_of,
)
from tests._tool_fakes import FAKE_ISSUE_URL_BASE, FakeGh

_PR = "https://github.com/owner/repo/pull/7"


def _entries(count: int, **overrides: object) -> list[DebtEntry]:
    return [
        _debt_entry(summary=f"finding number {n}", **overrides) for n in range(count)
    ]


def _new_body(count: int = 2, part: int = 1) -> str:
    ctx = _debt_ctx()
    items = [render_item(entry, ctx) for entry in _entries(count)]
    return render_body(items, ctx, part)


def _bodies(gh: FakeGh) -> list[str]:
    return [body for _, body in sorted(gh.open_issues.items())]


class TestRender:
    def test_item_is_a_checklist_entry_with_its_own_key_marker(self) -> None:
        entry = _debt_entry()
        item = render_item(entry, _debt_ctx())
        lines = item.text.splitlines()
        assert lines[0] == (
            "- [ ] **SHOULD_FIX** `src/cw/foo.py`:12 - Helper swallows the error"
        )
        assert "  - **Why deferred:** Out of scope for this ticket" in lines
        assert f"  {debt_marker(entry.key)}" in lines
        assert "  ```text" in lines
        assert "  except Exception:" in lines

    def test_rule_id_is_listed(self) -> None:
        item = render_item(_debt_entry(rule_id="py.bare-except"), _debt_ctx())
        assert "  - **Rule:** py.bare-except" in item.text.splitlines()

    def test_missing_evidence_gets_the_placeholder(self) -> None:
        item = render_item(_debt_entry(evidence="  "), _debt_ctx())
        assert "(evidence not recorded)" in item.text

    def test_a_longer_backtick_run_in_evidence_lengthens_the_fence(self) -> None:
        item = render_item(_debt_entry(evidence="a ```` b\n\nc"), _debt_ctx())
        assert "  `````text" in item.text.splitlines()
        # the blank evidence line stays blank, not "  "
        assert "" in item.text.splitlines()

    def test_free_text_cannot_forge_a_marker_line(self) -> None:
        forged = debt_marker("f" * 64)
        item = render_item(
            _debt_entry(summary=forged, rationale=forged, evidence=forged),
            _debt_ctx(),
        )
        lines = [line.strip() for line in item.text.splitlines()]
        assert forged not in lines
        assert sum(1 for line in lines if line.startswith("<!-- cw-review-debt")) == 1

    def test_multiline_and_long_fields_are_flattened_and_capped(self) -> None:
        item = render_item(
            _debt_entry(
                summary="one\ntwo",
                rationale="r " * ROLLUP_FIELD_MAX_CHARS,
                evidence="e" * (ROLLUP_EVIDENCE_MAX_CHARS + 50),
            ),
            _debt_ctx(),
        )
        assert "- Helper" not in item.text
        assert " - one two" in item.text.splitlines()[0]
        assert item.text.count("...") == 1
        assert "(evidence truncated)" in item.text
        assert len(item.text) < ROLLUP_BODY_MAX_CHARS // 4

    def test_item_text_is_redacted_but_the_marker_is_not(self) -> None:
        entry = _debt_entry(rationale="see /home/alice/secret")
        item = render_item(entry, _debt_ctx())
        assert "/home/alice" not in item.text
        assert debt_marker(entry.key) in item.text

    def test_body_keeps_the_legacy_first_line_and_the_part_marker(self) -> None:
        body = _new_body()
        assert body.splitlines()[0] == f"{scope_prefix('#3041')} `dev/3041`."
        assert queue_ticket_ref(body) == "3041"
        assert rollup_marker(1) in body.splitlines()
        assert body.rstrip().endswith("<!-- cw-agent-authored -->")

    def test_title_counts_findings_and_must_fix(self) -> None:
        ctx = _debt_ctx()
        items = [render_item(e, ctx) for e in _entries(3)]
        items.append(render_item(_debt_entry(severity="MUST_FIX", summary="m"), ctx))
        group = RollupGroup(None, 1, "", items)
        assert group_title(group, ctx) == (
            "review-debt: #3041 — 4 deferred findings (1 MUST_FIX)"
        )

    def test_title_singular_part_and_no_must_fix(self) -> None:
        ctx = _debt_ctx()
        group = RollupGroup(None, 2, "", [render_item(_debt_entry(), ctx)])
        assert group_title(group, ctx) == (
            "review-debt: #3041 — part 2, 1 deferred finding"
        )


class TestParse:
    def test_part_number_and_scope(self) -> None:
        body = _new_body(part=3)
        assert rollup_part(body) == 3
        assert is_rollup(body)
        assert scoped_rollup(body, "#3041") == 3
        assert scoped_rollup(body, "#304") is None
        assert scoped_rollup(body, "#9") is None

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "plain body",
            f"{scope_prefix('#3041')} `b`.\n\n{debt_marker('k' * 64)}\n",
            "<!-- cw-review-debt-rollup: part=0 -->",
            "<!-- cw-review-debt-rollup: part=x -->",
            "<!-- cw-review-debt-rollup: nope -->",
        ],
    )
    def test_legacy_and_malformed_bodies_are_not_rollups(self, body: str) -> None:
        assert rollup_part(body) is None
        assert not is_rollup(body)

    def test_scoped_rollup_of_an_empty_body(self) -> None:
        assert scoped_rollup("", "#3041") is None


class TestAppend:
    def test_items_go_before_the_pr_markers_and_the_agent_marker(self) -> None:
        body = _new_body(1) + pr_marker(_PR) + "\n"
        new = render_item(_debt_entry(summary="late"), _debt_ctx())
        out = append_items(body, [new])
        assert out.index("late") < out.index(pr_marker(_PR))
        assert out.rstrip().endswith(pr_marker(_PR))
        assert parse_pr_markers(out) == [_PR]
        assert out.count("<!-- cw-agent-authored -->") == 1

    def test_checked_items_are_untouched(self) -> None:
        body = _new_body(1).replace("- [ ]", "- [x]")
        out = append_items(body, [render_item(_debt_entry(summary="x"), _debt_ctx())])
        assert "- [x] **SHOULD_FIX**" in out
        assert out.count("- [ ]") == 1

    def test_a_body_with_no_tail_and_an_empty_body(self) -> None:
        item = render_item(_debt_entry(), _debt_ctx())
        assert append_items("head line", [item]).startswith("head line\n\n- [ ]")
        assert append_items("", [item]) == item.text


class TestPlanGroups:
    def test_no_items_no_groups(self) -> None:
        assert plan_groups([], _debt_ctx(), None) == []

    def test_everything_fits_one_new_part(self) -> None:
        ctx = _debt_ctx()
        items = [render_item(e, ctx) for e in _entries(3)]
        (group,) = plan_groups(items, ctx, None)
        assert (group.number, group.part, len(group.items)) == (None, 1, 3)

    def test_items_append_to_the_existing_target_while_they_fit(self) -> None:
        ctx = _debt_ctx()
        items = [render_item(e, ctx) for e in _entries(2)]
        (group,) = plan_groups(items, ctx, (55, 2, _new_body(1, part=2)))
        assert (group.number, group.part, len(group.items)) == (55, 2, 2)

    def test_overflow_opens_numbered_continuation_parts(self) -> None:
        ctx = _debt_ctx()
        big = [
            render_item(_debt_entry(summary=f"f{n}", evidence="x" * 5000), ctx)
            for n in range(30)
        ]
        groups = plan_groups(big, ctx, None)
        assert [g.part for g in groups] == list(range(1, len(groups) + 1))
        assert len(groups) > 1
        assert sum(len(g.items) for g in groups) == 30
        assert all(g.number is None for g in groups)
        for group in groups:
            assert len(render_body(group.items, ctx, group.part)) <= (
                ROLLUP_BODY_MAX_CHARS
            )

    def test_a_full_target_is_left_alone_and_a_next_part_opens(self) -> None:
        ctx = _debt_ctx()
        full = "x" * ROLLUP_BODY_MAX_CHARS
        items = [render_item(e, ctx) for e in _entries(2)]
        (group,) = plan_groups(items, ctx, (55, 2, full))
        assert (group.number, group.part, len(group.items)) == (None, 3, 2)

    def test_a_target_that_takes_some_then_overflows(self) -> None:
        ctx = _debt_ctx()
        items = [
            render_item(_debt_entry(summary=f"f{n}", evidence="y" * 5000), ctx)
            for n in range(4)
        ]
        room = ROLLUP_BODY_MAX_CHARS - 2 * len(items[0].text) - 10
        target = (55, 1, "z" * room)
        groups = plan_groups(items, ctx, target)
        assert [(g.number, g.part, len(g.items)) for g in groups] == [
            (55, 1, 2),
            (None, 2, 2),
        ]


class TestFiling:
    def test_one_issue_for_many_findings(self) -> None:
        gh = FakeGh()
        entries = _entries(52)
        result = file_review_debt(entries, _debt_ctx(), gh)
        assert len(gh.open_issues) == 1
        assert (
            gh.calls.count("create_issue:review-debt: #3041 — 52 deferred findings")
            == 1
        )
        assert {r.ticket_id for r in result.records} == {"100"}
        assert len(result.records) == 52
        assert len(_events_of(OrchestratorEventType.REVIEW_DEBT_FILED)) == 52

    def test_a_second_pass_appends_only_new_findings(self) -> None:
        gh = FakeGh()
        first = file_review_debt(_entries(2), _debt_ctx(), gh)
        ctx = _debt_ctx(existing=tuple(first.records))
        more = _entries(4)
        second = file_review_debt(more, ctx, gh)
        assert [r.key for r in second.records] == [e.key for e in more[2:]]
        assert {r.ticket_id for r in second.records} == {"100"}
        assert second.records[0].ticket_url == f"{FAKE_ISSUE_URL_BASE}100"
        assert len(gh.open_issues) == 1
        body = gh.open_issues[100]
        assert all(debt_marker(e.key) in body for e in more)
        assert body.count("- [ ]") == 4
        assert "fetch_issue_body:100" in gh.calls
        assert gh.calls.count("update_issue_body:100") == 1
        assert second.deduped_keys == (more[0].key, more[1].key)

    def test_rerunning_is_idempotent(self) -> None:
        gh = FakeGh()
        entries = _entries(3)
        first = file_review_debt(entries, _debt_ctx(), gh)
        before = dict(gh.open_issues)
        again = file_review_debt(entries, _debt_ctx(existing=tuple(first.records)), gh)
        assert again.records == []
        assert gh.open_issues == before
        assert "update_issue_body:100" not in gh.calls

    def test_marker_recovery_on_a_fresh_row_does_not_append(self) -> None:
        gh = FakeGh()
        entries = _entries(3)
        file_review_debt(entries, _debt_ctx(), gh)
        before = dict(gh.open_issues)
        again = file_review_debt(entries, _debt_ctx(existing=()), gh)
        assert [r.ticket_id for r in again.records] == ["100"] * 3
        assert again.deduped_keys == tuple(e.key for e in entries)
        assert gh.open_issues == before
        via = {e["via"] for e in _events_of(OrchestratorEventType.REVIEW_DEBT_DEDUPED)}
        assert via == {"marker"}

    def test_a_checked_off_item_is_not_re_added(self) -> None:
        gh = FakeGh()
        entries = _entries(2)
        file_review_debt(entries, _debt_ctx(), gh)
        gh.open_issues[100] = gh.open_issues[100].replace("- [ ]", "- [x]")
        gh.bodies[100] = gh.open_issues[100]
        result = file_review_debt(entries, _debt_ctx(), gh)
        assert result.records[0].ticket_id == "100"
        assert gh.open_issues[100].count("- [x]") == 2
        assert "update_issue_body:100" not in gh.calls

    def test_the_reread_keeps_a_box_ticked_since_the_listing(self) -> None:
        gh = FakeGh()
        first = file_review_debt(_entries(1), _debt_ctx(), gh)

        def _tick_before_the_reread(name: str) -> None:
            if name == "fetch_issue_body":
                gh.bodies[100] = gh.bodies[100].replace("- [ ]", "- [x]")

        gh.on_call = _tick_before_the_reread
        file_review_debt(_entries(2), _debt_ctx(existing=tuple(first.records)), gh)
        assert gh.open_issues[100].count("- [x]") == 1
        assert gh.open_issues[100].count("- [ ]") == 1

    def test_pr_markers_survive_an_append(self) -> None:
        gh = FakeGh()
        file_review_debt(_entries(1), _debt_ctx(), gh)
        gh.open_issues[100] += pr_marker(_PR) + "\n"
        gh.bodies[100] = gh.open_issues[100]
        file_review_debt(_entries(2), _debt_ctx(), gh)
        assert parse_pr_markers(gh.open_issues[100]) == [_PR]
        assert gh.open_issues[100].count("- [ ]") == 2

    def test_overflow_continues_in_a_numbered_issue(self) -> None:
        gh = FakeGh()
        entries = [
            _debt_entry(summary=f"finding {n}", evidence="x" * 5000) for n in range(30)
        ]
        result = file_review_debt(entries, _debt_ctx(), gh)
        assert len(gh.open_issues) > 1
        assert result.failures == []
        parts = sorted(rollup_part(b) or 0 for b in gh.open_issues.values())
        assert parts == list(range(1, len(parts) + 1))
        assert all(len(b) <= ROLLUP_BODY_MAX_CHARS for b in gh.open_issues.values())
        titles = [c for c in gh.calls if c.startswith("create_issue")]
        assert any("part 2" in t for t in titles)
        assert len(result.records) == 30
        # a later pass appends to the NEWEST part
        newest = max(gh.open_issues)
        extra = _debt_entry(summary="late arrival")
        later = file_review_debt(
            [*entries, extra], _debt_ctx(existing=tuple(result.records)), gh
        )
        assert [r.ticket_id for r in later.records] == [str(newest)]
        assert debt_marker(extra.key) in gh.open_issues[newest]

    def test_a_legacy_issue_still_dedupes_and_is_never_edited(self) -> None:
        gh = FakeGh()
        a, b = _entries(2)
        legacy = render_legacy_body(a, _debt_ctx())
        gh.open_issues[77] = legacy
        gh.bodies[77] = legacy
        result = file_review_debt([a, b], _debt_ctx(), gh)
        assert gh.open_issues[77] == legacy
        assert "update_issue_body:77" not in gh.calls
        assert result.deduped_keys == (a.key,)
        assert [(r.key, r.ticket_id) for r in result.records] == [
            (a.key, "77"),
            (b.key, "100"),
        ]
        # the new finding opened a roll-up; the legacy issue is not a target
        assert is_rollup(gh.open_issues[100])
        assert not is_rollup(gh.open_issues[77])

    def test_a_legacy_issue_for_another_ticket_is_not_a_match(self) -> None:
        gh = FakeGh()
        a = _entries(1)[0]
        gh.open_issues[77] = render_legacy_body(a, _debt_ctx(ticket_ref="#9999"))
        result = file_review_debt([a], _debt_ctx(), gh)
        assert result.deduped_keys == ()
        assert [r.ticket_id for r in result.records] == ["100"]

    def test_another_tickets_rollup_is_not_an_append_target(self) -> None:
        gh = FakeGh()
        other = _debt_ctx(ticket_ref="#9999")
        item = render_item(_debt_entry(summary="other"), other)
        gh.open_issues[88] = render_body([item], other, 1)
        result = file_review_debt(_entries(1), _debt_ctx(), gh)
        assert [r.ticket_id for r in result.records] == ["100"]
        assert "update_issue_body:88" not in gh.calls

    def test_a_closed_rollup_is_a_miss_and_a_new_one_opens(self) -> None:
        gh = FakeGh()
        entries = _entries(2)
        first = file_review_debt(entries, _debt_ctx(), gh)
        gh.issue_states[100] = "CLOSED"
        gh.open_issues.pop(100)
        again = file_review_debt(entries, _debt_ctx(existing=tuple(first.records)), gh)
        assert gh.calls.count("fetch_issue_state:100") == 1  # once, not per record
        assert [r.ticket_id for r in again.records] == ["101", "101"]
        assert is_rollup(gh.open_issues[101])

    def test_row_dedup_reads_a_shared_ticket_state_once(self) -> None:
        gh = FakeGh()
        entries = _entries(3)
        records = tuple(_debt_record(key=e.key, ticket_id="55") for e in entries)
        result = file_review_debt(entries, _debt_ctx(existing=records), gh)
        assert gh.calls == ["fetch_issue_state:55"]
        assert result.deduped_keys == tuple(e.key for e in entries)

    def test_the_url_of_a_sibling_record_names_an_appended_issue(self) -> None:
        gh = FakeGh()
        first = file_review_debt(_entries(1), _debt_ctx(), gh)
        url = "https://github.com/owner/repo/issues/100"
        ctx = _debt_ctx(existing=tuple(first.records))
        result = file_review_debt(_entries(2), ctx, gh)
        assert result.records[0].ticket_url == url

    def test_a_marker_hit_without_a_sibling_url_falls_back_to_the_number(self) -> None:
        gh = FakeGh()
        entries = _entries(1)
        file_review_debt(entries, _debt_ctx(), gh)
        result = file_review_debt(entries, _debt_ctx(existing=()), gh)
        assert result.records[0].ticket_url == "#100"


class TestFilingFailures:
    def _seed(self) -> tuple[FakeGh, tuple[ReviewDebtRecord, ...]]:
        gh = FakeGh()
        first = file_review_debt(_entries(1), _debt_ctx(), gh)
        return gh, tuple(first.records)

    def test_a_failed_body_read_fails_the_appended_entries(self) -> None:
        gh, existing = self._seed()
        gh.fail_fetch_body = {"100"}
        result = file_review_debt(_entries(3), _debt_ctx(existing=existing), gh)
        assert result.records == []
        assert {f.kind for f in result.failures} == {FilingFailureKind.NETWORK}
        assert len(result.failures) == 2
        assert gh.open_issues[100].count("- [ ]") == 1

    def test_a_failed_body_update_fails_the_appended_entries(self) -> None:
        gh, existing = self._seed()
        gh.fail_update_issue_body_at = {0}
        result = file_review_debt(_entries(2), _debt_ctx(existing=existing), gh)
        assert result.records == []
        assert [f.kind for f in result.failures] == [FilingFailureKind.OTHER]
        retry = file_review_debt(_entries(2), _debt_ctx(existing=existing), gh)
        assert len(retry.records) == 1
        assert gh.open_issues[100].count("- [ ]") == 2

    def test_a_truncated_listing_fails_the_unmatched_entries_closed(self) -> None:
        gh, existing = self._seed()
        gh.list_truncated = True
        result = file_review_debt(_entries(2), _debt_ctx(existing=existing), gh)
        assert [f.kind for f in result.failures] == [FilingFailureKind.LOOKUP_FAILED]
        assert "update_issue_body:100" not in gh.calls

    def test_a_create_failure_in_a_continuation_keeps_the_earlier_parts(self) -> None:
        gh = FakeGh()
        entries = [
            _debt_entry(summary=f"finding {n}", evidence="x" * 5000) for n in range(30)
        ]
        creates: list[str] = []

        def _fail_the_second_create(name: str) -> None:
            if name == "create_issue":
                creates.append(name)
                gh.fail_create_kind = GhFailure.NETWORK if len(creates) == 2 else None

        gh.on_call = _fail_the_second_create
        result = file_review_debt(entries, _debt_ctx(), gh)
        listed = {r.key for r in result.records}
        failed = {f.key for f in result.failures}
        assert listed
        assert failed
        assert listed | failed == {e.key for e in entries}
        assert not listed & failed

    def test_budget_spent_before_the_body_read(self) -> None:
        clock = FakeClock()
        gh, existing = self._seed()
        gh.on_call = _clock_stepper(clock, 30.0)
        ctx = _debt_ctx(existing=existing, clock=clock, budget_s=90.0)
        result = file_review_debt(_entries(2), ctx, gh)
        # label 0->30, list 30->60, fetch 60->90: nothing left for the update.
        assert [f.kind for f in result.failures] == [FilingFailureKind.BUDGET_EXHAUSTED]

    def test_budget_spent_before_the_body_fetch(self) -> None:
        clock = FakeClock()
        gh, existing = self._seed()
        gh.on_call = _clock_stepper(clock, 45.0)
        ctx = _debt_ctx(existing=existing, clock=clock, budget_s=90.0)
        result = file_review_debt(_entries(2), ctx, gh)
        assert [f.kind for f in result.failures] == [FilingFailureKind.BUDGET_EXHAUSTED]
        assert "fetch_issue_body:100" not in gh.calls

    def test_a_failed_create_names_the_gh_failure_kind(self) -> None:
        gh = FakeGh()
        gh.fail_create_kind = GhFailure.AUTH
        result = file_review_debt(_entries(1), _debt_ctx(), gh)
        assert [f.kind for f in result.failures] == [FilingFailureKind.AUTH]
