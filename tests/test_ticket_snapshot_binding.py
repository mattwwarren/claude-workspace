"""The one trust evaluator for snapshot comments: R3 rows B1-B13 (RFC 0019 T2, #3712).

B9 (the finalize-findings carve-out) is a docs rule evaluated by the model; its
pin lives in ``tests/test_finalize_findings_comment.py`` and
``tests/test_impl_guard_staleness_docs.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.codex_fix_loop.constraints import RESOLUTIONS_MARKER, select_constraint_comment
from cw.gh import _PLAN_MARKER, agent_marked_body
from cw.models import ClientConfig
from cw.models.bindings import Bindings
from cw.ticket_snapshot import (
    DEFAULT_SNAPSHOT_CONTEXT,
    Purpose,
    SnapshotContext,
    bound_comments,
    context_for_client,
    read_for_stage,
)
from tests._shared.bindings_builders import bindings_github_cli, bindings_linear_mcp
from tests._shared.outbox_builders import client_config
from tests._shared.ticket_context import make_comment, make_snapshot_from
from tests._ticket_view_helpers import make_task

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.ticket_snapshot import SnapshotComment, TicketSnapshot

OPERATOR = "operator"
PLAN_BODY = f"{_PLAN_MARKER} -->\nthe approved plan\n"
RESOLUTIONS_BODY = f"{RESOLUTIONS_MARKER}\nDo not add `new_lock.py`.\n"
ALL_PURPOSES = tuple(Purpose)


def _snapshot(*comments: SnapshotComment) -> TicketSnapshot:
    return make_snapshot_from(comments)


def _mcp(*operator_ids: str) -> SnapshotContext:
    return SnapshotContext(operator_ids=tuple(operator_ids), github=False)


def _github(*operator_ids: str) -> SnapshotContext:
    return SnapshotContext(operator_ids=tuple(operator_ids), github=True)


def _ids(rows: Sequence[dict[str, object]]) -> list[object]:
    return [row["id"] for row in rows]


# --- B1: operator text, GitHub ---


def test_operator_text_github_any_author_ignores_operator_ids() -> None:
    snapshot = _snapshot(
        make_comment("op", author_id=OPERATOR),
        make_comment("other", author_id="stranger"),
        make_comment("anon", author_id=None, author=None),
        make_comment("agent", agent_authored=True),
        make_comment("empty", body=""),
        make_comment("blank", body="  \n\t "),
    )
    # operator_ids is non-empty and matches nobody; GitHub ignores it entirely.
    for ctx in (_github(), _github("someone-else")):
        rows = bound_comments(snapshot, ctx, Purpose.OPERATOR_TEXT)
        assert _ids(rows) == ["op", "other", "anon"]


# --- B2: operator text, MCP ---


def test_operator_text_mcp_binds_only_operator_ids() -> None:
    snapshot = _snapshot(
        make_comment("exact", author_id="U-1"),
        make_comment("folded", author_id="u-1"),
        make_comment("stranger", author_id="U-2"),
        make_comment("agent", author_id="U-1", agent_authored=True),
        make_comment("empty", author_id="U-1", body=""),
        make_comment("blank", author_id="U-1", body="  \n"),
    )
    rows = bound_comments(snapshot, _mcp("U-1"), Purpose.OPERATOR_TEXT)
    assert _ids(rows) == ["exact", "folded"]


def test_operator_ids_are_casefolded_at_evaluation_not_only_the_comment() -> None:
    snapshot = _snapshot(make_comment("c", author_id="u-1"))
    rows = bound_comments(snapshot, _mcp("U-1"), Purpose.OPERATOR_TEXT)
    assert _ids(rows) == ["c"]


# --- B3: voided and settle, GitHub ---


def test_voided_dispositions_github_all_bodies() -> None:
    snapshot = _snapshot(
        make_comment("op", author_id=OPERATOR),
        make_comment("agent", agent_authored=True),
        make_comment("stranger", author_id="stranger"),
        make_comment("empty", body=""),
    )
    rows = bound_comments(snapshot, _github(), Purpose.VOIDED_AND_SETTLE)
    assert _ids(rows) == ["op", "agent", "stranger", "empty"]


# --- B4: voided and settle, MCP ---


def test_voided_dispositions_mcp_bound_only() -> None:
    snapshot = _snapshot(
        make_comment("op", author_id="U-1"),
        make_comment("folded", author_id="u-1"),
        make_comment("agent", author_id="U-1", agent_authored=True),
        make_comment("stranger", author_id="U-2"),
        make_comment("empty", author_id="U-1", body=""),
    )
    rows = bound_comments(snapshot, _mcp("U-1"), Purpose.VOIDED_AND_SETTLE)
    # Same predicate as B2; an empty body is a valid settle/voided carrier.
    assert _ids(rows) == ["op", "folded", "empty"]


# --- B5/B6: constraints ---


def _constraint(
    snapshot: TicketSnapshot, ctx: SnapshotContext, login: str | None
) -> str | None:
    rows = bound_comments(snapshot, ctx, Purpose.CONSTRAINTS)
    chosen = select_constraint_comment(
        rows, operator_login=login, ticket_text=None, prefiltered=not ctx.github
    )
    return None if chosen is None else str(chosen["id"])


def test_constraints_github_rule_unchanged() -> None:
    snapshot = _snapshot(
        make_comment(
            "old", body=RESOLUTIONS_BODY, author_id="Op-Login", created_at="2026-01-02"
        ),
        make_comment(
            "newest",
            body=RESOLUTIONS_BODY,
            author_id="Op-Login",
            created_at="2026-01-09",
        ),
        make_comment(
            "agent",
            body=agent_marked_body(RESOLUTIONS_BODY),
            author_id="Op-Login",
            created_at="2026-02-01",
        ),
        make_comment(
            "header",
            body=f"## Blocking Review Findings\n{RESOLUTIONS_BODY}",
            author_id="Op-Login",
            created_at="2026-03-01",
        ),
        make_comment(
            "stranger",
            body=RESOLUTIONS_BODY,
            author_id="stranger",
            created_at="2026-04-01",
        ),
        make_comment(
            "unmarked",
            body="plain comment",
            author_id="Op-Login",
            created_at="2026-05-01",
        ),
    )
    ctx = _github("ignored-id")
    assert _constraint(snapshot, ctx, "Op-Login") == "newest"
    # Exact-case compare: a differently cased login does not match on GitHub.
    assert _constraint(snapshot, ctx, "op-login") is None
    # An unresolved login fails closed.
    assert _constraint(snapshot, ctx, None) is None


def test_constraints_mcp_requires_operator_id_and_uses_snapshot_order() -> None:
    snapshot = _snapshot(
        # Earlier in snapshot order but with the lexically LARGER created_at.
        make_comment(
            "earlier-bigger",
            body=RESOLUTIONS_BODY,
            author_id="U-1",
            created_at="2026-12-31T00:00:00Z",
        ),
        make_comment(
            "later-smaller",
            body=RESOLUTIONS_BODY,
            author_id="u-1",
            created_at="2026-01-01T00:00:00Z",
        ),
        make_comment(
            "stranger",
            body=RESOLUTIONS_BODY,
            author_id="U-2",
            created_at="2027-01-01T00:00:00Z",
        ),
        make_comment(
            "agent",
            body=agent_marked_body(RESOLUTIONS_BODY),
            author_id="U-1",
            agent_authored=True,
        ),
        make_comment(
            "header",
            body=f"## Blocking Review Findings\n{RESOLUTIONS_BODY}",
            author_id="U-1",
        ),
    )
    ctx = _mcp("U-1")
    assert _constraint(snapshot, ctx, None) == "later-smaller"
    assert _constraint(snapshot, _mcp(), None) is None


# --- B7/B8: approved plan ---


def test_plan_github_trusts_gh_login_ignores_agent_marker() -> None:
    snapshot = _snapshot(
        make_comment("first", body=PLAN_BODY, author_id="gh-login"),
        make_comment(
            "agent", body=PLAN_BODY, author_id="gh-login", agent_authored=True
        ),
        make_comment("wrong-case", body=PLAN_BODY, author_id="GH-LOGIN"),
        make_comment("other", body=PLAN_BODY, author_id="stranger"),
        make_comment("no-marker", body="just words", author_id="gh-login"),
        make_comment("anon", body=PLAN_BODY, author_id=None),
    )
    ctx = _github("ignored")
    rows = bound_comments(snapshot, ctx, Purpose.APPROVED_PLAN, gh_login="gh-login")
    assert _ids(rows) == ["first", "agent"]
    # No resolved gh login: nothing binds.
    assert bound_comments(snapshot, ctx, Purpose.APPROVED_PLAN) == []


def test_plan_mcp_agent_marker_ignored_operator_id_required() -> None:
    snapshot = _snapshot(
        make_comment("plain", body=PLAN_BODY, author_id="U-1"),
        make_comment("agent", body=PLAN_BODY, author_id="u-1", agent_authored=True),
        make_comment("stranger", body=PLAN_BODY, author_id="U-2"),
        make_comment("no-marker", body="not a plan", author_id="U-1"),
        make_comment("anon", body=PLAN_BODY, author_id=None),
    )
    rows = bound_comments(snapshot, _mcp("U-1"), Purpose.APPROVED_PLAN)
    assert _ids(rows) == ["plain", "agent"]


# --- B10/B11/B12 ---


@pytest.mark.parametrize("author_id", [None, "", "  "])
@pytest.mark.parametrize(
    ("purpose", "body"),
    [
        (Purpose.OPERATOR_TEXT, "text"),
        (Purpose.VOIDED_AND_SETTLE, "text"),
        (Purpose.CONSTRAINTS, RESOLUTIONS_BODY),
        (Purpose.APPROVED_PLAN, PLAN_BODY),
    ],
)
def test_null_or_blank_author_id_never_binds(
    author_id: str | None, purpose: Purpose, body: str
) -> None:
    snapshot = _snapshot(make_comment("c", body=body, author_id=author_id))
    # A blank id never matches even an operator_ids entry that is also blank-ish.
    assert bound_comments(snapshot, _mcp("U-1", " "), purpose) == []


@pytest.mark.parametrize("purpose", ALL_PURPOSES)
def test_empty_operator_ids_binds_nothing_on_mcp(purpose: Purpose) -> None:
    snapshot = _snapshot(
        make_comment("a", body=PLAN_BODY + RESOLUTIONS_BODY, author_id="U-1"),
        make_comment("b", body=PLAN_BODY + RESOLUTIONS_BODY, author_id="operator"),
    )
    assert bound_comments(snapshot, _mcp(), purpose) == []


@pytest.mark.parametrize("purpose", ALL_PURPOSES)
def test_author_conflict_is_untrusted_all_purposes(purpose: Purpose) -> None:
    body = PLAN_BODY + RESOLUTIONS_BODY
    snapshot = _snapshot(
        make_comment("flagged", body=body, author_id="U-1", author_conflict=True),
        make_comment("clean", body=body, author_id="U-1"),
    )
    rows = bound_comments(snapshot, _mcp("U-1"), purpose)
    assert _ids(rows) == ["clean"]


# --- B13: legacy dict shape and casefold scope ---


def test_legacy_dict_shape_and_casefold_only_on_mcp() -> None:
    snapshot = _snapshot(
        make_comment(
            "c1",
            body="hello",
            author_id="User-ID",
            author="Display Name",
            created_at="2026-11-18T12:00:00Z",
        ),
        make_comment("c2", body="anon", author_id=None, author=None),
    )
    mcp_rows = bound_comments(snapshot, _mcp("user-id"), Purpose.VOIDED_AND_SETTLE)
    assert mcp_rows == [
        {
            "id": "c1",
            "body": "hello",
            "author": {"login": "Display Name"},
            "createdAt": "2026-11-18T12:00:00Z",
        }
    ]
    github_rows = bound_comments(snapshot, _github(), Purpose.VOIDED_AND_SETTLE)
    assert github_rows[1]["author"] is None
    assert github_rows[0]["author"] == {"login": "Display Name"}
    # GitHub plan compare is exact: only the identical-case login binds.
    plan = _snapshot(make_comment("p", body=PLAN_BODY, author_id="Gh-Login"))
    assert (
        bound_comments(plan, _github(), Purpose.APPROVED_PLAN, gh_login="gh-login")
        == []
    )
    assert _ids(
        bound_comments(plan, _github(), Purpose.APPROVED_PLAN, gh_login="Gh-Login")
    ) == ["p"]
    # MCP casefolds: the same case difference binds on an operator id.
    assert _ids(bound_comments(plan, _mcp("gh-login"), Purpose.APPROVED_PLAN)) == ["p"]


def test_none_snapshot_binds_nothing() -> None:
    for purpose in ALL_PURPOSES:
        assert bound_comments(None, _github(), purpose, gh_login="x") == []
        assert bound_comments(None, _mcp("U-1"), purpose) == []


# --- SnapshotContext defaults ---


def test_default_snapshot_context_is_fail_closed() -> None:
    ctx = DEFAULT_SNAPSHOT_CONTEXT
    assert ctx == SnapshotContext()
    assert ctx.github is False
    assert ctx.operator_ids == ()
    assert ctx.operator_login is None
    assert ctx.resolve is read_for_stage
    assert ctx.comments_readable is False
    snapshot = _snapshot(make_comment("c", author_id="operator"))
    for purpose in ALL_PURPOSES:
        assert bound_comments(snapshot, ctx, purpose) == []


@pytest.mark.parametrize(
    ("github", "ids", "readable"),
    [(False, (), False), (False, ("U",), True), (True, (), True), (True, ("U",), True)],
)
def test_comments_readable_follows_github_or_operator_ids(
    github: bool, ids: tuple[str, ...], readable: bool
) -> None:
    assert (
        SnapshotContext(operator_ids=ids, github=github).comments_readable is readable
    )


# --- context_for_client ---


def _client(tmp_path: Path, bindings: Bindings, tracker: str | None) -> ClientConfig:
    return client_config(tmp_path, bindings=bindings, tracker=tracker).model_copy(
        update={"operator_github_login": "configured-login"}
    )


def test_context_for_client_github_flag_comes_from_the_tracker_config(
    tmp_path: Path,
) -> None:
    gh = context_for_client(
        _client(tmp_path / "gh", bindings_github_cli(), "github-issues"),
        tmp_path,
        resolve_login=False,
    )
    other = context_for_client(
        _client(tmp_path / "linear", bindings_github_cli(), "linear"),
        tmp_path,
        resolve_login=False,
    )
    unset = context_for_client(
        _client(tmp_path / "unset", bindings_github_cli(), None),
        tmp_path,
        resolve_login=False,
    )
    assert gh.github is True
    assert other.github is False
    assert unset.github is False


def test_context_for_client_takes_operator_ids_from_bindings(tmp_path: Path) -> None:
    client = _client(
        tmp_path, bindings_linear_mcp(operator_ids=["U-1", "u-2"]), "linear"
    )
    ctx = context_for_client(client, tmp_path, resolve_login=False)
    assert ctx.operator_ids == ("U-1", "u-2")
    assert ctx.comments_readable is True


def test_context_for_client_without_a_tickets_binding_has_no_ids(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, Bindings(), None)
    ctx = context_for_client(client, tmp_path, resolve_login=False)
    assert ctx.operator_ids == ()
    assert ctx.comments_readable is False


def test_context_for_client_resolves_login_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, bindings_github_cli(), "github-issues")
    assert (
        context_for_client(client, tmp_path, resolve_login=True).operator_login
        == "configured-login"
    )
    calls: list[str] = []

    def _resolve(candidate: ClientConfig) -> str:
        calls.append(candidate.name)
        return "x"

    monkeypatch.setattr("cw.ticket_snapshot._binding.resolve_operator_login", _resolve)
    assert (
        context_for_client(client, tmp_path, resolve_login=False).operator_login is None
    )
    assert calls == []


def test_context_for_client_default_resolver_reads_without_refreshing(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, bindings_github_cli(), "github-issues")
    ctx = context_for_client(client, tmp_path, resolve_login=False)
    assert ctx.resolve is read_for_stage
    assert ctx.resolve(make_task(), "review") is None


def test_context_for_client_refresh_resolver_calls_snapshot_for_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, bindings_github_cli(), "github-issues")
    seen: list[tuple[object, object, object, str]] = []
    sentinel = _snapshot(make_comment("c"))

    def _fake(
        task: object, client_arg: object, worktree: object, *, stage: str
    ) -> TicketSnapshot:
        seen.append((task, client_arg, worktree, stage))
        return sentinel

    monkeypatch.setattr("cw.ticket_snapshot._binding.snapshot_for_stage", _fake)
    ctx = context_for_client(client, tmp_path, resolve_login=False, refresh=True)
    task = make_task()
    assert ctx.resolve is not read_for_stage
    assert ctx.resolve(task, "review") is sentinel
    assert seen == [(task, client, tmp_path, "review")]
