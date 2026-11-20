"""Plan-of-record recovery for codex IMPL (RFC 0019 T2 R13, #3712)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from freezegun import freeze_time

from cw import outbox
from cw.exceptions import CwError
from cw.models import ReviewFeedback
from cw.outbox import OutboxRecordError, OutboxRole, OutboxState
from cw.ticket_snapshot import SnapshotContext
from cw.ticket_snapshot import _plan as plan_mod
from cw.ticket_snapshot._plan import PLAN_RECOVERED_FRICTION, recover_plan
from cw.tracker_post import plan_parts
from tests._shared.outbox_builders import CLIENT, client_config, enqueue
from tests._shared.ticket_context import make_comment, make_snapshot_from
from tests._ticket_view_helpers import ACME_TICKET, make_task

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask
    from cw.ticket_snapshot import TicketSnapshot

PLAN_HEAD = "<!-- plan-spec-reviewed: 2026-11-19 -->\n"
PLAN_TEXT = PLAN_HEAD + "# Plan\n\nStep one: café ✓\n"
OTHER_PLAN = PLAN_HEAD + "# Plan v2\n\nStep one, revised.\n"
MCP = SnapshotContext(operator_ids=("U-1",), github=False)
T1 = "2026-11-19T10:00:00Z"
T2 = "2026-11-19T11:00:00Z"
T3 = "2026-11-19T12:00:00Z"


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    path.mkdir()
    return path


@pytest.fixture
def client(tmp_path: Path) -> ClientConfig:
    return client_config(tmp_path / "ws", tracker="linear")


def _plan_path(worktree: Path) -> Path:
    return worktree / ".cw" / "plan.md"


def _queue(stamp: str, parts: list[str], **overrides: Any) -> None:
    fields: dict[str, Any] = {
        "role": OutboxRole.TICKETS,
        "ticket": ACME_TICKET,
        "marker": "plan-s1",
        "producer": "codex-plan",
    }
    fields.update(overrides)
    with freeze_time(stamp):
        enqueue(parts=parts, **fields)


def _snapshot(*bodies: str, author_id: str = "U-1") -> TicketSnapshot:
    comments = [
        make_comment(f"c{n}", body=body, author_id=author_id)
        for n, body in enumerate(bodies)
    ]
    return make_snapshot_from(comments)


def _recover(
    worktree: Path,
    client: ClientConfig,
    *,
    snapshot: TicketSnapshot | None = None,
    ctx: SnapshotContext = MCP,
    task: TicketTask | None = None,
) -> str | None:
    return recover_plan(
        task if task is not None else make_task(), client, worktree, snapshot, ctx
    )


def test_friction_constant_is_the_agreed_token() -> None:
    assert PLAN_RECOVERED_FRICTION == "plan_recovered"


# --- outbox first ---


def test_outbox_single_part_is_written_verbatim_with_mkdir_and_utf8(
    worktree: Path, client: ClientConfig
) -> None:
    _queue(T1, [PLAN_TEXT])
    assert not (worktree / ".cw").exists()
    assert _recover(worktree, client) == "outbox"
    assert _plan_path(worktree).read_bytes() == PLAN_TEXT.encode("utf-8")


def test_a_pending_entry_recovers(worktree: Path, client: ClientConfig) -> None:
    _queue(T1, [PLAN_TEXT])
    (entry,) = outbox.list_entries(CLIENT)
    assert entry.state is OutboxState.PENDING
    assert _recover(worktree, client) == "outbox"


def test_the_newest_of_two_plan_entries_wins(
    worktree: Path, client: ClientConfig
) -> None:
    _queue(T1, [PLAN_TEXT], marker="plan-old")
    _queue(T2, [OTHER_PLAN], marker="plan-new")
    assert _recover(worktree, client) == "outbox"
    assert _plan_path(worktree).read_text(encoding="utf-8") == OTHER_PLAN


def test_a_park_marker_entry_is_ignored(worktree: Path, client: ClientConfig) -> None:
    _queue(T1, [PLAN_TEXT], marker="plan-s1")
    _queue(T2, [OTHER_PLAN], marker="park-s2")
    assert _recover(worktree, client) == "outbox"
    assert _plan_path(worktree).read_text(encoding="utf-8") == PLAN_TEXT


def test_only_a_park_entry_recovers_nothing(
    worktree: Path, client: ClientConfig
) -> None:
    _queue(T1, [PLAN_TEXT], marker="park-s1")
    assert _recover(worktree, client) is None
    assert not _plan_path(worktree).exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"ticket": "OTHER-9"},
        {"producer": "codex-review"},
        {"role": OutboxRole.KNOWLEDGE},
    ],
    ids=["other-ticket", "other-producer", "other-role"],
)
def test_entries_for_another_ticket_producer_or_role_are_ignored(
    worktree: Path, client: ClientConfig, overrides: dict[str, Any]
) -> None:
    _queue(T1, [PLAN_TEXT], **overrides)
    assert _recover(worktree, client) is None


def test_a_multi_part_entry_is_unwrapped(worktree: Path, client: ClientConfig) -> None:
    text = PLAN_HEAD + "".join(f"plan line {n} with words\n" for n in range(6000))
    plan = plan_parts(text, home="/home/nobody", worktrees=[])
    assert len(plan.bodies) > 1
    _queue(T1, list(plan.bodies))
    assert _recover(worktree, client) == "outbox"
    assert _plan_path(worktree).read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    "parts",
    [
        ["(part 1 of 2)\n\nfirst", "no label here"],
        ["(part 1 of 3)\n\nfirst", "(part 2 of 3)\n\nsecond"],
        ["(part 2 of 2)\n\nfirst", "(part 1 of 2)\n\nsecond"],
    ],
    ids=["unlabelled-part", "wrong-total", "swapped"],
)
def test_a_malformed_or_mixed_label_is_no_entry_and_falls_through_to_snapshot(
    worktree: Path, client: ClientConfig, parts: list[str]
) -> None:
    _queue(T1, parts)
    snapshot = _snapshot(PLAN_TEXT)
    assert _recover(worktree, client, snapshot=snapshot) == "snapshot"
    assert _plan_path(worktree).read_text(encoding="utf-8") == PLAN_TEXT


def test_a_malformed_entry_with_no_snapshot_recovers_nothing(
    worktree: Path, client: ClientConfig
) -> None:
    _queue(T1, ["(part 1 of 2)\n\nfirst", "no label here"])
    assert _recover(worktree, client) is None
    assert not _plan_path(worktree).exists()


def test_recovered_outbox_text_without_the_marker_is_rejected(
    worktree: Path, client: ClientConfig
) -> None:
    _queue(T1, ["# A plan nobody reviewed\n"])
    assert _recover(worktree, client) is None
    assert not _plan_path(worktree).exists()


def test_an_unreviewed_outbox_text_falls_through_to_a_reviewed_snapshot_plan(
    worktree: Path, client: ClientConfig
) -> None:
    _queue(T1, ["# A plan nobody reviewed\n"])
    assert _recover(worktree, client, snapshot=_snapshot(PLAN_TEXT)) == "snapshot"


@pytest.mark.parametrize(
    "error",
    [OSError("gone"), OutboxRecordError("corrupt"), CwError("other")],
    ids=["oserror", "outbox-record-error", "cw-error"],
)
def test_outbox_list_failure_falls_through_to_the_snapshot(
    worktree: Path,
    client: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    def _boom(_client: str) -> list[object]:
        raise error

    monkeypatch.setattr(plan_mod, "list_entries", _boom)
    with caplog.at_level(logging.WARNING, logger="cw.ticket_snapshot"):
        source = _recover(worktree, client, snapshot=_snapshot(PLAN_TEXT))
    assert source == "snapshot"
    assert "outbox unreadable" in caplog.text
    assert f"error_class={type(error).__name__}" in caplog.text
    assert str(error) not in caplog.text


def test_outbox_body_read_failure_falls_through_to_the_snapshot(
    worktree: Path, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _queue(T1, [PLAN_TEXT])

    def _boom(_entry: object) -> list[str]:
        msg = "body missing"
        raise OutboxRecordError(msg)

    monkeypatch.setattr(plan_mod, "read_parts", _boom)
    assert _recover(worktree, client, snapshot=_snapshot(OTHER_PLAN)) == "snapshot"
    assert _plan_path(worktree).read_text(encoding="utf-8") == OTHER_PLAN


def test_an_entry_with_no_parts_is_no_entry(
    worktree: Path, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _queue(T1, [PLAN_TEXT])
    monkeypatch.setattr(plan_mod, "read_parts", lambda _entry: [])
    assert _recover(worktree, client) is None


# --- snapshot second ---


def test_snapshot_recovers_the_last_bound_plan_comment_and_ignores_agent_marker(
    worktree: Path, client: ClientConfig
) -> None:
    snapshot = make_snapshot_from(
        [
            make_comment("c1", body=PLAN_TEXT, author_id="U-1"),
            make_comment("c2", body=OTHER_PLAN, author_id="u-1", agent_authored=True),
            make_comment("c3", body=PLAN_HEAD + "stranger", author_id="U-2"),
            make_comment("c4", body="no marker at all", author_id="U-1"),
        ]
    )
    assert _recover(worktree, client, snapshot=snapshot) == "snapshot"
    assert _plan_path(worktree).read_text(encoding="utf-8") == OTHER_PLAN


def test_snapshot_without_a_bound_plan_comment_recovers_nothing(
    worktree: Path, client: ClientConfig
) -> None:
    snapshot = _snapshot(PLAN_TEXT, author_id="U-2")
    assert _recover(worktree, client, snapshot=snapshot) is None
    assert _recover(worktree, client, snapshot=None) is None
    assert not _plan_path(worktree).exists()


def test_github_snapshot_plan_binds_the_current_gh_login_only(
    worktree: Path, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plan_mod, "cached_gh_login", lambda: "gh-login")
    ctx = SnapshotContext(github=True)
    snapshot = make_snapshot_from(
        [
            make_comment("c1", body=OTHER_PLAN, author_id="stranger"),
            make_comment("c2", body=PLAN_TEXT, author_id="gh-login"),
        ]
    )
    assert _recover(worktree, client, snapshot=snapshot, ctx=ctx) == "snapshot"
    assert _plan_path(worktree).read_text(encoding="utf-8") == PLAN_TEXT


def test_github_snapshot_plan_with_unresolved_gh_login_recovers_nothing(
    worktree: Path, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plan_mod, "cached_gh_login", lambda: None)
    snapshot = _snapshot(PLAN_TEXT, author_id="gh-login")
    ctx = SnapshotContext(github=True)
    assert _recover(worktree, client, snapshot=snapshot, ctx=ctx) is None


def test_non_github_never_resolves_the_gh_login(
    worktree: Path, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> str:
        msg = "gh api user must stay lazy"
        raise AssertionError(msg)

    monkeypatch.setattr(plan_mod, "cached_gh_login", _boom)
    assert _recover(worktree, client, snapshot=_snapshot(PLAN_TEXT)) == "snapshot"


def test_snapshot_plan_without_the_marker_is_never_a_candidate(
    worktree: Path, client: ClientConfig
) -> None:
    assert (
        _recover(worktree, client, snapshot=_snapshot("# plan without marker")) is None
    )


def test_the_outbox_newest_entry_wins_even_when_the_ticket_holds_a_newer_plan(
    worktree: Path, client: ClientConfig
) -> None:
    # Bounded staleness (accepted): a later round posted straight on the ticket
    # (or unqueued) is not seen once an outbox entry exists.
    _queue(T1, [PLAN_TEXT])
    newer_on_ticket = _snapshot(OTHER_PLAN)
    assert _recover(worktree, client, snapshot=newer_on_ticket) == "outbox"
    assert _plan_path(worktree).read_text(encoding="utf-8") == PLAN_TEXT


# --- guards that skip recovery ---


def test_a_local_plan_md_wins_and_is_left_untouched(
    worktree: Path, client: ClientConfig
) -> None:
    _plan_path(worktree).parent.mkdir()
    _plan_path(worktree).write_text("local plan", encoding="utf-8")
    _queue(T1, [PLAN_TEXT])
    assert _recover(worktree, client, snapshot=_snapshot(OTHER_PLAN)) is None
    assert _plan_path(worktree).read_text(encoding="utf-8") == "local plan"


def test_an_adopted_repair_entry_skips_recovery(
    worktree: Path, client: ClientConfig
) -> None:
    feedback = ReviewFeedback(
        repo="owner/repo",
        pr_number=1,
        reviewed_sha="a" * 40,
        captured_at=datetime(2026, 11, 19, tzinfo=UTC),
        report="fix it",
    )
    repair = make_task(review_feedback=feedback, branch="feat/adopted")
    _queue(T1, [PLAN_TEXT])
    assert (
        _recover(worktree, client, snapshot=_snapshot(OTHER_PLAN), task=repair) is None
    )
    assert not _plan_path(worktree).exists()


def test_review_feedback_without_a_branch_is_not_a_repair_entry(
    worktree: Path, client: ClientConfig
) -> None:
    feedback = ReviewFeedback(
        repo="owner/repo",
        pr_number=1,
        reviewed_sha="b" * 40,
        captured_at=datetime(2026, 11, 19, tzinfo=UTC),
        report="fix it",
    )
    _queue(T1, [PLAN_TEXT])
    task = make_task(review_feedback=feedback)
    assert _recover(worktree, client, task=task) == "outbox"


def test_nothing_recoverable_leaves_the_plan_absent(
    worktree: Path, client: ClientConfig
) -> None:
    assert _recover(worktree, client) is None
    assert not _plan_path(worktree).exists()


def test_recovery_warns_with_source_and_ticket_but_never_the_plan_text(
    worktree: Path, client: ClientConfig, caplog: pytest.LogCaptureFixture
) -> None:
    _queue(T1, [PLAN_TEXT])
    with caplog.at_level(logging.WARNING, logger="cw.ticket_snapshot"):
        _recover(worktree, client)
    assert f"ticket_id={ACME_TICKET} source=outbox" in caplog.text
    assert "Step one" not in caplog.text
