"""Tests for cw.codex_fix_loop.constraints — binding operator constraints (#2633)."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

import pytest

from cw.codex_fix_loop.baseline import cycle_touched_paths
from cw.codex_fix_loop.constraints import (
    RESOLUTIONS_MARKER,
    SECTION_HEADING,
    SECTION_MAX_CHARS,
    constraint_breach,
    constraints_from_comment,
    extract_forbidden,
    find_violations,
    render_section,
    select_constraint_comment,
)
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT
from cw.codex_fix_loop.growth import AdditionKind
from cw.codex_fix_loop.posted_text import WITHHELD
from cw.codex_review import CODEX_FIX_CONSTRAINT_VIOLATION
from tests._codex_review_helpers import _head_baseline, _observed_comments, _write
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.codex_fix_loop.constraints import ConstraintViolation, OperatorConstraints
    from cw.codex_fix_loop.fence import FenceBreach

_LOGIN = "operator-login"
_RESOLUTIONS_HEADER = "## Pre-flight Resolutions (operator)"
_ROUND_2_PREFIX = "Round-2 plan approved"
_SCAN_HEADER = "## Pending Verification Scan"
_ITEM_8 = (
    "Do not attach up to 4,000 characters of hook output to `blocker.details`: "
    "a secret-scanning hook (gitleaks, detect-secrets, trufflehog) could echo a "
    "matched secret into a public ticket comment."
)
_ITEM_10 = (
    "No ADR edits, no `# noqa`, no `# type: ignore`, no new per-file-ignore, "
    "no new lock, no subprocess under `sessions_lock`."
)


def _body(comment: dict[str, object]) -> str:
    body = comment["body"]
    assert isinstance(body, str)
    return body


def _resolutions() -> dict[str, object]:
    return next(
        c for c in _observed_comments() if _body(c).startswith(_RESOLUTIONS_HEADER)
    )


def _select(
    comments: list[dict[str, object]],
    *,
    operator_login: str | None = _LOGIN,
    ticket_text: str | None = None,
) -> dict[str, object] | None:
    return select_constraint_comment(
        comments, operator_login=operator_login, ticket_text=ticket_text
    )


class TestObservedFixture:
    def test_fixture_keeps_the_observed_key_set(self) -> None:
        keys = {
            "author",
            "authorAssociation",
            "body",
            "createdAt",
            "id",
            "includesCreatedEdit",
            "isMinimized",
            "minimizedReason",
            "reactionGroups",
            "url",
            "viewerDidAuthor",
        }
        comments = _observed_comments()

        assert len(comments) == 4
        assert all(set(c) == keys for c in comments)
        assert all(c["author"] == {"login": _LOGIN} for c in comments)


class TestSelectConstraintComment:
    def test_marker_comment_by_operator_selected(self) -> None:
        selected = _select(_observed_comments())

        assert selected is not None
        assert _body(selected).startswith(_RESOLUTIONS_HEADER)

    def test_unmarked_operator_comment_ignored(self) -> None:
        comments = [
            c for c in _observed_comments() if _body(c).startswith(_ROUND_2_PREFIX)
        ]

        assert len(comments) == 1
        assert _select(comments) is None

    def test_agent_authored_comment_quoting_the_marker_is_ignored(self) -> None:
        scans = [c for c in _observed_comments() if _body(c).startswith(_SCAN_HEADER)]
        # Hand-built boundary case: the same capture without its fixed header,
        # so only the agent-authored marker can exclude it.
        headerless = copy.deepcopy(scans[0])
        headerless["body"] = _body(scans[0]).removeprefix(_SCAN_HEADER)

        assert len(scans) == 2
        assert RESOLUTIONS_MARKER in _body(scans[0])
        assert "auto-dev-preflight-resolutions" in _body(scans[1])
        assert _select(scans) is None
        assert _select([headerless]) is None

    def test_other_author_ignored(self) -> None:
        assert _select(_observed_comments(), operator_login="someone-else") is None

    @pytest.mark.parametrize(
        "header", ["## Multi-Marker Gate Blocked", "## Blocking Review Findings"]
    )
    def test_fixed_header_comment_ignored(self, header: str) -> None:
        """Hand-built boundary case: a marker-bearing pipeline fixed header."""
        comment: dict[str, object] = {
            "author": {"login": _LOGIN},
            "body": f"{header}\n\nDo not add `foo_bar`.\n{RESOLUTIONS_MARKER}",
            "createdAt": "2026-10-08T00:00:00Z",
        }

        assert _select([comment]) is None

    def test_newest_marker_comment_wins(self) -> None:
        later = copy.deepcopy(_resolutions())
        later["createdAt"] = "2026-10-09T00:00:00Z"
        later["id"] = "IC_kwDOPLACEHOLDER000000099"

        selected = _select([*_observed_comments(), later])

        assert selected is not None
        assert selected["id"] == "IC_kwDOPLACEHOLDER000000099"

    def test_unresolved_login_fails_closed(self) -> None:
        assert _select(_observed_comments(), operator_login=None) is None

    def test_body_marker_in_ticket_text_defers_to_ticket_context(self) -> None:
        ticket = f"Ticket body.\n{RESOLUTIONS_MARKER}"

        assert _select(_observed_comments(), ticket_text=ticket) is None

    def test_sparse_comments_do_not_raise(self) -> None:
        """Hand-built boundary cases the capture cannot show."""
        sparse: list[dict[str, object]] = [
            {"author": None, "body": RESOLUTIONS_MARKER},
            {"author": {"login": _LOGIN}, "body": 7},
            {
                "author": {"login": _LOGIN},
                "body": f"Do not add `x`.\n{RESOLUTIONS_MARKER}",
            },
        ]

        selected = _select(sparse)

        assert selected is sparse[2]
        assert constraints_from_comment(selected).created_at is None
        assert constraints_from_comment({"body": None}).body == ""


class TestExtractForbidden:
    @pytest.mark.parametrize(
        "text",
        [
            "Do not add `foo_bar`.",
            "don't introduce `foo_bar`.",
            "don\N{RIGHT SINGLE QUOTATION MARK}t introduce `foo_bar`.",
            "do not create `foo_bar`.",
            "Do not re-add `foo_bar`.",
            "do not build `foo_bar`.",
            "must not add `foo_bar`.",
            "Never create `foo_bar`.",
            "no new `foo_bar` module.",
            "DO NOT ADD `foo_bar`.",
        ],
    )
    def test_each_pinned_phrase_matches(self, text: str) -> None:
        assert extract_forbidden(text) == (frozenset({"foo_bar"}), frozenset())

    @pytest.mark.parametrize(
        "text",
        [
            "must not build `foo_bar`.",
            "remove `foo_bar`.",
            "drop `foo_bar`.",
            "without `foo_bar`.",
            "delete `foo_bar`.",
            "do not remove `foo_bar`.",
        ],
    )
    def test_phrases_outside_the_pinned_set_do_not_match(self, text: str) -> None:
        assert extract_forbidden(text) == (frozenset(), frozenset())

    @pytest.mark.parametrize(
        "text", ["redo not add `foo_bar`.", "casino new `foo_bar`."]
    )
    def test_word_bounded(self, text: str) -> None:
        assert extract_forbidden(text) == (frozenset(), frozenset())

    @pytest.mark.parametrize(
        "text",
        [
            "Do not add this. Use `foo_bar` instead.",
            "Do not add this\n`foo_bar` instead.",
        ],
    )
    def test_sentence_scoped(self, text: str) -> None:
        assert extract_forbidden(text) == (frozenset(), frozenset())

    @pytest.mark.parametrize(
        ("span", "expected"),
        [
            ("`foo_bar()`", {"foo_bar"}),
            ("` foo_bar.`", {"foo_bar"}),  # a "." + space would end the sentence
            ("`src/cw/x.py`", {"src/cw/x.py"}),
            ("`blocker.details`", {"blocker.details"}),
            ("`# noqa`", set()),
            ("`a b`", set()),
            ("`ab`", set()),
        ],
    )
    def test_backtick_strip_and_shape_rules(
        self, span: str, expected: set[str]
    ) -> None:
        tokens, _kinds = extract_forbidden(f"Do not add {span} here.")

        assert tokens == expected

    def test_lock_keyword_yields_lock_kind(self) -> None:
        assert extract_forbidden("no new lock.")[1] == {AdditionKind.LOCK}
        assert extract_forbidden("no new `sessions_lock` user.")[1] == frozenset()

    @pytest.mark.parametrize("text", ["do not add a state file.", "no new sidecar."])
    def test_state_phrases_yield_state_kind(self, text: str) -> None:
        assert extract_forbidden(text)[1] == {AdditionKind.STATE_FILE}

    def test_worked_example_resolution_8_yields_nothing(self) -> None:
        """`attach` is not a pinned verb, so `blocker.details` is never forbidden."""
        assert extract_forbidden(_ITEM_8) == (frozenset(), frozenset())

    def test_worked_example_resolution_10_yields_sessions_lock_and_lock_kind(
        self,
    ) -> None:
        assert extract_forbidden(_ITEM_10) == (
            frozenset({"sessions_lock"}),
            frozenset({AdditionKind.LOCK}),
        )

    def test_whole_observed_resolutions_comment(self) -> None:
        """`TicketTask` is the known false positive (item 9's quoted list name)."""
        body = _body(_resolutions())

        assert _ITEM_8 in body
        assert extract_forbidden(body) == (
            frozenset({"TicketTask", "sessions_lock"}),
            frozenset({AdditionKind.LOCK}),
        )


def _constraints(text: str, created_at: str | None = None) -> OperatorConstraints:
    comment: dict[str, object] = {
        "author": {"login": _LOGIN},
        "body": f"{text}\n{RESOLUTIONS_MARKER}",
        "id": "IC_kwDOPLACEHOLDER000000042",
    }
    if created_at is not None:
        comment["createdAt"] = created_at
    return constraints_from_comment(comment)


class TestPromptSection:
    def test_heading_and_marker_line_stripped(self) -> None:
        constraints = constraints_from_comment(_resolutions())

        section = render_section(constraints)

        assert section is not None
        assert section.startswith(SECTION_HEADING)
        assert _RESOLUTIONS_HEADER in section
        assert RESOLUTIONS_MARKER not in section
        assert constraints.created_at == "2026-10-07T22:06:12Z"

    def test_size_cap_with_truncation_note(self) -> None:
        section = render_section(_constraints("x" * (SECTION_MAX_CHARS + 500)))

        assert section is not None
        assert "x" * (SECTION_MAX_CHARS + 1) not in section
        assert "[truncated" in section

    def test_none_without_constraints(self) -> None:
        assert render_section(None) is None


def _repo(make_git_repo: Callable[..., Path], base: str = "a = 1\n") -> Path:
    repo = make_git_repo("constraints")
    _write(repo / "src" / "a.py", base)
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", "base")
    return repo


def _violations(
    repo: Path, constraints: OperatorConstraints
) -> list[ConstraintViolation]:
    baseline = _head_baseline(repo)
    cycle_touched_paths(repo, baseline)  # stages the cycle, as the loop does
    return find_violations(repo, baseline, constraints)


def _breach(repo: Path, constraints: OperatorConstraints) -> FenceBreach | None:
    baseline = _head_baseline(repo)
    cycle_touched_paths(repo, baseline)
    return constraint_breach(repo, baseline, constraints, cycle=3)


_FOO = _constraints("Do not add `foo_bar`.")


class TestFindViolations:
    def test_new_definition_of_forbidden_token_is_violation(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "src" / "a.py", "a = 1\ndef foo_bar():\n    pass\n")

        found = _violations(repo, _FOO)

        assert [(v.path, v.line, v.label) for v in found] == [
            ("src/a.py", 2, "foo_bar")
        ]

    def test_forbidden_token_new_to_the_file_is_violation(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "src" / "a.py", "from x import foo_bar\na = foo_bar()\n")

        found = _violations(repo, _FOO)

        assert [v.line for v in found] == [1, 2]

    def test_token_in_a_file_the_cycle_creates_is_violation(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "src" / "b.py", "def foo_bar():\n    pass\n")

        found = _violations(repo, _FOO)

        assert [(v.path, v.line) for v in found] == [("src/b.py", 1)]

    def test_new_file_whose_path_matches_the_token_is_violation(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "src" / "outbox.py", "x = 1\n")

        found = _violations(repo, _constraints("no new `outbox.py` module."))

        assert [(v.path, v.line, v.label) for v in found] == [
            ("src/outbox.py", None, "outbox.py")
        ]

    def test_token_the_file_already_contained_never_parks_an_edit(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo, "def foo_bar():\n    return 1\n")
        _write(repo / "src" / "a.py", "def foo_bar():\n    return foo_bar or 2\n")

        assert _violations(repo, _FOO) == []

    def test_word_boundary(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "src" / "a.py", "mode_filename = 1\nxmode_file = 2\n")

        assert _violations(repo, _constraints("Do not add `mode_file`.")) == []

    def test_renamed_lock_caught_by_kind(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "src" / "a.py", "a = 1\n_guard = threading.Lock()\n")

        found = _violations(repo, _constraints("no new lock."))

        assert [(v.line, v.label) for v in found] == [(2, AdditionKind.LOCK.value)]

    def test_existing_lock_line_edited_is_not_a_violation(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo, "_L = threading.Lock()\n")
        _write(repo / "src" / "a.py", "_L = threading.Lock()  # guards a\n")

        assert _violations(repo, _constraints("no new lock.")) == []

    def test_test_files_ignored(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "tests" / "test_a.py", "def foo_bar():\n    pass\n")
        _write(repo / "tests" / "foo_bar", "x\n")

        assert _violations(repo, _FOO) == []

    def test_removed_line_is_not_violation(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo, "a = 1\nfoo_bar = 2\n")
        _write(repo / "src" / "a.py", "a = 1\n")

        assert _violations(repo, _FOO) == []


class TestConstraintBreach:
    def test_breach_cites_constraint_label_and_file_line(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "src" / "a.py", "a = 1\ndef foo_bar():\n    pass\n")
        constraints = _constraints("Do not add `foo_bar`.", "2026-10-07T22:06:12Z")

        breach = _breach(repo, constraints)

        assert breach is not None
        assert (
            "Constraint (from the operator's auto-dev-preflight-resolutions comment, "
            'dated 2026-10-07T22:06:12Z): "Do not add `foo_bar`."'
        ) in breach.details
        assert "- src/a.py:2 adds foo_bar: def foo_bar():" in breach.details
        assert breach.paths == ("src/a.py",)

    def test_new_file_line_and_undated_constraint(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "src" / "outbox.py", "x = 1\n")

        breach = _breach(repo, _constraints("no new `outbox.py` module."))

        assert breach is not None
        assert "- src/outbox.py (new file) adds outbox.py" in breach.details
        assert "comment): " in breach.details

    def test_added_line_goes_through_describe_added_line(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        long_line = "foo_bar = compute()  # " + "z" * 300
        _write(
            repo / "src" / "a.py",
            f'a = 1\nfoo_bar_password = "hunter2"; foo_bar = 1\n{long_line}\n',
        )

        breach = _breach(repo, _FOO)

        assert breach is not None
        assert f"- src/a.py:2 adds foo_bar: {WITHHELD}" in breach.details
        assert "hunter2" not in breach.details
        assert "z" * 121 not in breach.details

    def test_breach_hint_has_leftstaged_and_guard_switch(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "src" / "a.py", "a = 1\nfoo_bar = 1\n")

        breach = _breach(repo, _FOO)

        assert breach is not None
        assert breach.recovery_hint is not None
        assert LEFT_STAGED_HINT in breach.recovery_hint
        assert "codex_fix_loop_growth_guard_enabled" in breach.recovery_hint

    def test_reason_is_constraint_violation(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "src" / "a.py", "a = 1\nfoo_bar = 1\n")

        breach = _breach(repo, _FOO)

        assert breach is not None
        assert breach.reason == CODEX_FIX_CONSTRAINT_VIOLATION

    def test_clean_cycle_returns_none(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "src" / "a.py", "a = 2\n")

        assert _breach(repo, _FOO) is None
