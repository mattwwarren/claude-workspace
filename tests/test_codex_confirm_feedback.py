"""Tests for cw.codex_confirm_feedback — confirm findings back into the loop (#2807)."""

from __future__ import annotations

import itertools
import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.claude_review import CLAUDE_REVIEWER_LABEL
from cw.codex_clean_confirm import CleanConfirmOutcome
from cw.codex_confirm_feedback import (
    ConfirmFeedback,
    feedback_token,
    load_confirm_feedback,
    reenter_feedback,
    reentry_enabled,
    with_feedback_tokens,
)
from cw.codex_fix_loop import FIX_CYCLE_FLOOR_SECONDS
from cw.codex_review import (
    CODEX_MUST_FIX_FINDINGS,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_REVIEWER_LABEL,
    make_codex_blocked,
)
from cw.models import ClientConfig, Stage, TicketTask
from cw.models.client import ReviewConfig
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    SettleTarget,
    operator_settle,
    round_verdict_path,
    save_round_verdict,
)
from tests._review_convergence_helpers import (
    NOW,
    consolidated_verdict,
    dirty_confirm_ledger,
    fresh_ledger,
    live_merge_base,
    record,
    round_finding,
    stamp_dispositions,
    write_ledger,
)
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.review_convergence import ConvergenceLedger
    from cw.review_findings import ReviewVerdict

_TICKET = "T-1"


def _task() -> TicketTask:
    return TicketTask(ticket_id=_TICKET, client="test", stage=Stage.REVIEW)


def _client(
    worktree: Path, *, convergence: str = "clean", reentry: bool = True
) -> ClientConfig:
    return ClientConfig(
        name="test",
        workspace_path=worktree,
        default_branch="main",
        review=ReviewConfig.model_validate(
            {"convergence": convergence, "confirm_reentry": reentry}
        ),
    )


def _head(worktree: Path) -> str:
    return git_in(worktree, "rev-parse", "HEAD")


def _claude_verdict(worktree: Path, *accepted: tuple[str, str]) -> ReviewVerdict:
    """A Claude confirm verdict at HEAD with ``(severity, summary)`` findings."""
    return consolidated_verdict(
        accepted=list(accepted) or [("MUST_FIX", "claude finding")],
        sha=_head(worktree),
    )


def _load(
    worktree: Path,
    *,
    client: ClientConfig | None = None,
    fix_loop_enabled: bool = True,
    after_round: int = 0,
) -> ConfirmFeedback | None:
    return load_confirm_feedback(
        task=_task(),
        worktree=worktree,
        client=client or _client(worktree),
        fix_loop_enabled=fix_loop_enabled,
        after_round=after_round,
    )


def _summaries(feedback: ConfirmFeedback | None) -> list[str]:
    assert feedback is not None
    return [item.finding.summary for item in feedback.findings]


@pytest.fixture
def worktree(make_git_repo_with_origin: Callable[..., tuple[Path, Path]]) -> Path:
    repo, _origin = make_git_repo_with_origin("wt-confirm-feedback")
    return repo


# ---------------------------------------------------------------------------
# reentry_enabled — the single read site of review.confirm_reentry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("convergence", "reentry", "fix_loop", "expected"),
    [
        (
            convergence,
            reentry,
            fix_loop,
            (convergence, reentry, fix_loop) == ("clean", True, True),
        )
        for convergence, reentry, fix_loop in itertools.product(
            ("clean", "blocking"), (True, False), (True, False)
        )
    ],
)
def test_reentry_enabled_only_for_clean_opted_in_with_the_loop_on(
    tmp_path: Path, convergence: str, reentry: bool, fix_loop: bool, expected: bool
) -> None:
    client = _client(tmp_path, convergence=convergence, reentry=reentry)
    assert reentry_enabled(client, fix_loop_enabled=fix_loop) is expected


@pytest.mark.parametrize(
    ("convergence", "reentry", "fix_loop"),
    [("blocking", True, True), ("clean", False, True), ("clean", True, False)],
    ids=["blocking", "opt-out", "fix-loop-off"],
)
def test_a_disabled_reader_does_no_ledger_io(
    tmp_path: Path, convergence: str, reentry: bool, fix_loop: bool
) -> None:
    client = _client(tmp_path, convergence=convergence, reentry=reentry)
    with (
        patch("cw.codex_confirm_feedback.load_ledger") as load_mock,
        patch("cw.codex_confirm_feedback.capture_head_sha") as head_mock,
    ):
        assert _load(tmp_path, client=client, fix_loop_enabled=fix_loop) is None
    load_mock.assert_not_called()
    head_mock.assert_not_called()


# ---------------------------------------------------------------------------
# load_confirm_feedback — the reader
# ---------------------------------------------------------------------------


def test_a_dirty_confirm_at_head_yields_its_must_fix(worktree: Path) -> None:
    ledger = dirty_confirm_ledger(worktree, _claude_verdict(worktree))
    feedback = _load(worktree)
    assert feedback is not None
    assert feedback.round == ledger.rounds[-1].round
    assert _summaries(feedback) == ["claude finding"]


def test_no_ledger_yields_nothing(worktree: Path) -> None:
    assert _load(worktree) is None


def test_another_tickets_ledger_yields_nothing(worktree: Path) -> None:
    dirty_confirm_ledger(worktree, _claude_verdict(worktree), ticket_id="T-other")
    assert _load(worktree) is None


@pytest.mark.parametrize(
    "update",
    [{"fork_point": "0" * 40}, {"loop_backend": "claude-native"}],
    ids=["fork-mismatch", "not-codex"],
)
def test_a_foreign_ledger_yields_nothing(
    worktree: Path, update: dict[str, object]
) -> None:
    ledger = dirty_confirm_ledger(worktree, _claude_verdict(worktree))
    write_ledger(worktree, ledger.model_copy(update=update))
    assert _load(worktree) is None


def test_a_stopped_ledger_yields_nothing(worktree: Path) -> None:
    ledger = dirty_confirm_ledger(worktree, _claude_verdict(worktree))
    write_ledger(
        worktree,
        ledger.model_copy(update={"status": "stopped", "stop_reason": "max_cycles"}),
    )
    assert _load(worktree) is None


def test_a_converged_ledger_yields_nothing(worktree: Path) -> None:
    ledger = dirty_confirm_ledger(worktree, _claude_verdict(worktree))
    write_ledger(
        worktree,
        ledger.model_copy(
            update={"status": "converged", "last_clean_sha": _head(worktree)}
        ),
    )
    assert _load(worktree) is None


def _codex_ledger(worktree: Path) -> ConvergenceLedger:
    return fresh_ledger(
        confirm="claude",
        loop_backend="codex",
        ticket_id=_TICKET,
        fork_point=live_merge_base(worktree),
    )


def test_an_awaiting_confirm_ledger_yields_nothing(worktree: Path) -> None:
    ledger, report = record(
        _codex_ledger(worktree), sha=_head(worktree), backend="codex"
    )
    assert report.decision == "confirm_required"
    write_ledger(worktree, ledger)
    assert _load(worktree) is None


def test_a_dirty_loop_round_is_not_confirm_feedback(worktree: Path) -> None:
    ledger, report = record(
        _codex_ledger(worktree), round_finding(), sha=_head(worktree), backend="codex"
    )
    assert report.decision == "continue"
    write_ledger(worktree, ledger)
    assert _load(worktree) is None


def test_head_moved_since_the_confirm_yields_nothing(worktree: Path) -> None:
    dirty_confirm_ledger(worktree, _claude_verdict(worktree))
    commit_tracked_file(worktree, "moved.py")
    assert _load(worktree) is None


def test_a_missing_round_verdict_warns_and_yields_nothing(
    worktree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ledger = dirty_confirm_ledger(worktree, _claude_verdict(worktree))
    round_verdict_path(
        worktree / LEDGER_RELATIVE_PATH, ledger.rounds[-1].round
    ).unlink()
    with caplog.at_level(logging.WARNING, logger="cw.codex_confirm_feedback"):
        assert _load(worktree) is None
    assert any(
        "claude confirm feedback unreadable" in r.getMessage()
        and _TICKET in r.getMessage()
        for r in caplog.records
    )


def test_a_corrupt_round_verdict_warns_and_yields_nothing(
    worktree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ledger = dirty_confirm_ledger(worktree, _claude_verdict(worktree))
    path = round_verdict_path(worktree / LEDGER_RELATIVE_PATH, ledger.rounds[-1].round)
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="cw.codex_confirm_feedback"):
        assert _load(worktree) is None
    assert any("unreadable" in r.getMessage() for r in caplog.records)


def test_an_operator_settled_finding_is_dropped(worktree: Path) -> None:
    verdict = _claude_verdict(
        worktree, ("MUST_FIX", "settled finding"), ("MUST_FIX", "open finding")
    )
    ledger = dirty_confirm_ledger(worktree, verdict)
    settled_item = next(
        item for item in verdict.accepted if item.finding.summary == "settled finding"
    )
    settled, report = operator_settle(
        ledger,
        [
            SettleTarget(
                severity="MUST_FIX",
                file=settled_item.finding.file,
                summary=settled_item.finding.summary,
            )
        ],
        actor="operator",
        reason="false positive",
        now=NOW,
    )
    assert report.unmatched == []
    write_ledger(worktree, settled)
    assert _summaries(_load(worktree)) == ["open finding"]


def test_should_fix_is_never_seeded(worktree: Path) -> None:
    verdict = _claude_verdict(
        worktree, ("MUST_FIX", "must fix finding"), ("SHOULD_FIX", "should fix finding")
    )
    dirty_confirm_ledger(worktree, verdict)
    assert _summaries(_load(worktree)) == ["must fix finding"]


def test_an_operator_actionable_must_fix_is_not_seeded(worktree: Path) -> None:
    recorded = _claude_verdict(worktree, ("MUST_FIX", "loop work"))
    ledger = dirty_confirm_ledger(worktree, recorded)
    persisted = stamp_dispositions(
        _claude_verdict(
            worktree, ("MUST_FIX", "loop work"), ("MUST_FIX", "operator work")
        ),
        {"operator work": ("operator_actionable", "needs an operator")},
    )
    save_round_verdict(
        worktree / LEDGER_RELATIVE_PATH, ledger.rounds[-1].round, persisted
    )
    assert _summaries(_load(worktree)) == ["loop work"]


def test_a_confirm_round_with_nothing_left_to_seed_yields_nothing(
    worktree: Path,
) -> None:
    ledger = dirty_confirm_ledger(worktree, _claude_verdict(worktree))
    persisted = stamp_dispositions(
        _claude_verdict(worktree),
        {"claude finding": ("rejected", "operator rejected")},
    )
    save_round_verdict(
        worktree / LEDGER_RELATIVE_PATH, ledger.rounds[-1].round, persisted
    )
    assert _load(worktree) is None


def test_dropped_and_unresolved_must_fix_are_seeded(worktree: Path) -> None:
    verdict = stamp_dispositions(
        _claude_verdict(
            worktree,
            ("MUST_FIX", "dropped finding"),
            ("MUST_FIX", "unresolved finding"),
        ),
        {
            "dropped finding": ("dropped", "no adjudication covered it"),
            "unresolved finding": ("unresolved", "survived the cap"),
        },
    )
    dirty_confirm_ledger(worktree, verdict)
    assert sorted(_summaries(_load(worktree))) == [
        "dropped finding",
        "unresolved finding",
    ]


def test_after_round_gates_an_already_consumed_round(worktree: Path) -> None:
    ledger = dirty_confirm_ledger(worktree, _claude_verdict(worktree))
    confirm_round = ledger.rounds[-1].round
    assert _load(worktree, after_round=confirm_round) is None
    assert _load(worktree, after_round=confirm_round - 1) is not None


# ---------------------------------------------------------------------------
# reenter_feedback — the post-gate decision
# ---------------------------------------------------------------------------


def _gated(
    worktree: Path,
    *,
    label: str = CLAUDE_REVIEWER_LABEL,
    reason: str | None = CODEX_MUST_FIX_FINDINGS,
) -> CleanConfirmOutcome:
    if reason is None:
        result = make_codex_blocked(
            ticket_id=_TICKET, worktree=worktree, reason=CODEX_MUST_FIX_FINDINGS
        ).model_copy(update={"blocker": None, "status": "stage_complete"})
    else:
        result = make_codex_blocked(ticket_id=_TICKET, worktree=worktree, reason=reason)
    return CleanConfirmOutcome(
        result=result, verdict=None, reviewer_label=label, fix_loop_footer=False
    )


def _reenter(
    worktree: Path,
    gated: CleanConfirmOutcome,
    *,
    client: ClientConfig | None = None,
    deadline: float | None = None,
    after_round: int = 0,
) -> ConfirmFeedback | None:
    return reenter_feedback(
        gated,
        task=_task(),
        worktree=worktree,
        client=client or _client(worktree),
        fix_loop_enabled=True,
        after_round=after_round,
        deadline=deadline,
    )


def test_a_claude_must_fix_park_re_enters(worktree: Path) -> None:
    dirty_confirm_ledger(worktree, _claude_verdict(worktree))
    assert _summaries(_reenter(worktree, _gated(worktree))) == ["claude finding"]


@pytest.mark.parametrize(
    ("label", "reason"),
    [
        (CODEX_REVIEWER_LABEL, CODEX_MUST_FIX_FINDINGS),
        (CLAUDE_REVIEWER_LABEL, CODEX_REVIEW_UNPARSEABLE),
        (CLAUDE_REVIEWER_LABEL, None),
    ],
    ids=["codex-park", "other-reason", "converged"],
)
def test_other_outcomes_never_touch_the_ledger(
    worktree: Path, label: str, reason: str | None
) -> None:
    dirty_confirm_ledger(worktree, _claude_verdict(worktree))
    with patch("cw.codex_confirm_feedback.load_ledger") as load_mock:
        assert _reenter(worktree, _gated(worktree, label=label, reason=reason)) is None
    load_mock.assert_not_called()


@pytest.mark.parametrize(
    ("convergence", "reentry"),
    [("blocking", True), ("clean", False)],
    ids=["blocking", "opt-out"],
)
def test_a_disabled_client_never_re_enters(
    worktree: Path, convergence: str, reentry: bool
) -> None:
    dirty_confirm_ledger(worktree, _claude_verdict(worktree))
    client = _client(worktree, convergence=convergence, reentry=reentry)
    with patch("cw.codex_confirm_feedback.load_ledger") as load_mock:
        assert _reenter(worktree, _gated(worktree), client=client) is None
    load_mock.assert_not_called()


def test_no_feedback_on_disk_does_not_re_enter(worktree: Path) -> None:
    assert _reenter(worktree, _gated(worktree)) is None


def test_a_consumed_round_does_not_re_enter(worktree: Path) -> None:
    ledger = dirty_confirm_ledger(worktree, _claude_verdict(worktree))
    gated = _gated(worktree)
    assert _reenter(worktree, gated, after_round=ledger.rounds[-1].round) is None


@pytest.mark.parametrize(
    ("remaining", "re_enters"),
    [
        (FIX_CYCLE_FLOOR_SECONDS - 1, False),
        (FIX_CYCLE_FLOOR_SECONDS, True),
        (FIX_CYCLE_FLOOR_SECONDS + 100, True),
    ],
    ids=["below-floor", "at-floor", "above-floor"],
)
def test_the_deadline_floor_gates_a_re_entry(
    worktree: Path,
    caplog: pytest.LogCaptureFixture,
    remaining: int,
    re_enters: bool,
) -> None:
    dirty_confirm_ledger(worktree, _claude_verdict(worktree))
    clock = MagicMock()
    clock.monotonic.return_value = 1000.0
    with (
        patch("cw.codex_confirm_feedback.time", clock),
        caplog.at_level(logging.WARNING, logger="cw.codex_confirm_feedback"),
    ):
        feedback = _reenter(worktree, _gated(worktree), deadline=1000.0 + remaining)
    assert (feedback is not None) is re_enters
    assert any("fix-cycle floor" in r.getMessage() for r in caplog.records) is (
        not re_enters
    )


def test_no_deadline_never_reads_the_clock(worktree: Path) -> None:
    dirty_confirm_ledger(worktree, _claude_verdict(worktree))
    clock = MagicMock()
    with patch("cw.codex_confirm_feedback.time", clock):
        assert _reenter(worktree, _gated(worktree), deadline=None) is not None
    clock.monotonic.assert_not_called()


# ---------------------------------------------------------------------------
# feedback_token / with_feedback_tokens — the caller-side friction
# ---------------------------------------------------------------------------


def _feedback(worktree: Path, count: int) -> ConfirmFeedback:
    verdict = _claude_verdict(
        worktree, *[("MUST_FIX", f"finding {n}") for n in range(count)]
    )
    return ConfirmFeedback(round=4, findings=tuple(verdict.accepted))


def test_feedback_token_names_the_round_and_the_finding_count(
    worktree: Path,
) -> None:
    assert (
        feedback_token(_feedback(worktree, 2))
        == "claude_confirm_feedback: round=4 findings=2"
    )


def test_with_feedback_tokens_appends_and_revalidates(worktree: Path) -> None:
    result = make_codex_blocked(
        ticket_id=_TICKET, worktree=worktree, reason=CODEX_MUST_FIX_FINDINGS
    ).model_copy(update={"friction_highlights": ["existing"]})
    merged = with_feedback_tokens(result, ["token a", "token b"])
    assert merged.friction_highlights == ["existing", "token a", "token b"]
    assert AutoDevResult.model_validate(merged.model_dump(mode="json")) == merged
    assert result.friction_highlights == ["existing"]


def test_with_feedback_tokens_without_tokens_is_the_same_result(
    worktree: Path,
) -> None:
    result = make_codex_blocked(
        ticket_id=_TICKET, worktree=worktree, reason=CODEX_MUST_FIX_FINDINGS
    )
    assert with_feedback_tokens(result, []) is result
