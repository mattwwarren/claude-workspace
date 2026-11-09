"""Tests for the review-debt title, body and payload rendering (#3041, R8)."""

from __future__ import annotations

from cw.gh import AGENT_COMMENT_MARKER
from cw.review_debt_filing import debt_marker, file_review_debt
from cw.review_debt_filing._render import (
    render_body,
    render_payload,
    render_title,
)
from tests._fake_gh import FakeGh
from tests._review_debt_helpers import _debt_ctx, _debt_entry


def test_title_is_severity_and_a_truncated_summary() -> None:
    entry = _debt_entry(summary="x" * 100)
    assert render_title(entry, _debt_ctx()) == f"review-debt: SHOULD_FIX {'x' * 80}"


def test_title_collapses_whitespace() -> None:
    entry = _debt_entry(summary="two\n  lines")
    assert render_title(entry, _debt_ctx()) == "review-debt: SHOULD_FIX two lines"


def test_body_matches_the_verbatim_template() -> None:
    entry = _debt_entry(rule_id="py.swallow")
    body = render_body(entry, _debt_ctx())
    assert body == (
        "Deferred during review of #3041 on branch `dev/3041`.\n"
        "\n"
        "**Severity:** SHOULD_FIX\n"
        "**File:** `src/cw/foo.py`:12\n"
        "**Rule:** py.swallow\n"
        "**Why deferred:** Out of scope for this ticket\n"
        "\n"
        "Helper swallows the error\n"
        "\n"
        "```text\n"
        "except Exception:\n"
        "    pass\n"
        "```\n"
        "\n"
        f"{debt_marker(entry.key)}\n"
        f"{AGENT_COMMENT_MARKER}\n"
    )


def test_body_omits_the_rule_and_line_when_absent() -> None:
    body = render_body(_debt_entry(line=None), _debt_ctx())
    assert "**Rule:**" not in body
    assert "**File:** `src/cw/foo.py`\n" in body


def test_missing_evidence_still_produces_a_fence() -> None:
    body = render_body(_debt_entry(evidence=""), _debt_ctx())
    assert "```text\n(evidence not recorded)\n```\n" in body


def test_evidence_with_a_fence_gets_a_longer_one() -> None:
    body = render_body(_debt_entry(evidence="a\n```\nb ````"), _debt_ctx())
    assert "`````text\na\n```\nb ````\n`````\n" in body


def test_paths_home_and_secrets_are_redacted_in_title_and_body() -> None:
    secret = "ghp_" + "A" * 36
    entry = _debt_entry(
        summary="leak at /srv/wt/acme/src/x.py",
        rationale=f"token {secret} under /home/alice/notes",
        evidence="/home/alice/.claude/projects/-home-alice-x/1.jsonl",
    )
    ctx = _debt_ctx()
    title = render_title(entry, ctx)
    body = render_body(entry, ctx)
    for text in (title, body):
        assert "/srv/wt/acme" not in text
        assert "/home/alice" not in text
        assert secret not in text
    assert "<worktree>/src/x.py" in title
    assert "<redacted>" in body


def test_payload_carries_key_marker_title_and_body() -> None:
    entry = _debt_entry()
    payload = render_payload(entry, _debt_ctx())
    assert payload["key"] == entry.key
    assert payload["marker"] == debt_marker(entry.key)
    assert payload["body"].endswith(
        f"{debt_marker(entry.key)}\n{AGENT_COMMENT_MARKER}\n"
    )


def test_undeterminable_visibility_refuses_to_file() -> None:
    gh = FakeGh()
    result = file_review_debt([_debt_entry()], _debt_ctx(visibility=None), gh)
    assert gh.calls == []
    assert [f.kind.value for f in result.failures] == ["visibility_undetermined"]
