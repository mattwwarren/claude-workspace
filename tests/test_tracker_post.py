"""Unit tests for :mod:`cw.tracker_post` (#2810).

The pure wrap/split/measure/read-back helpers and the ``post_parts``
orchestration behind ``cw post-comment``. The module's bindings of
``post_issue_comment``, ``fetch_issue_comments`` and ``time.sleep`` are
monkeypatched, so no real ``gh`` runs. The read-back tests port the guarantees
of the shell read-back fence the finalize appendix used to carry.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cw import tracker_post as tp
from cw.gh import AGENT_COMMENT_MARKER, agent_marked_body
from cw.review_adjudication import (
    parse_voided_findings_block,
    render_voided_findings_block,
)
from cw.review_adjudication._models import VoidedFinding

HOME = "/home/alice"
WORKTREE = "/home/alice/.cw/wt/abcd1234/dev-2810"
TICKET = "2810"
URL_BASE = "https://github.com/OWNER/REPO/issues/2810#issuecomment-"
FIXTURE = Path(__file__).parent / "fixtures" / "gh_issue_comments_2633.json"
FINALIZE_HEADER = "## Blocking Review Findings"
FINALIZE_SUBHEADING = "### Finalize ship-it gate findings"

type PostCalls = list[tuple[str, str, Path | None]]


def _observed_comments() -> list[dict[str, object]]:
    """The captured ``gh issue view --json comments`` list (redacted values)."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    comments: list[dict[str, object]] = data["comments"]
    return comments


def _comment(url: str, body: str) -> dict[str, object]:
    """An observed comment entry with its ``url`` and ``body`` replaced."""
    entry = dict(_observed_comments()[0])
    entry["url"] = url
    entry["body"] = body
    return entry


def _proc(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        ["gh"], returncode, stdout=stdout.encode(), stderr=b""
    )


class _FakeTracker:
    """A ticket thread: posts append comments; fetches return the thread."""

    def __init__(self) -> None:
        self.comments: list[dict[str, object]] = list(_observed_comments())
        self.posts: PostCalls = []
        self.fetches = 0
        self.sleeps: list[float] = []
        # Per-post overrides, consumed in order: a CompletedProcess, or None.
        self.results: list[subprocess.CompletedProcess[bytes] | None] = []
        # Rewrites the stored body (simulates tracker-side normalisation).
        self.store = agent_marked_body
        self.hide_until_fetch = 0

    def post(
        self, ticket_id: str, body: str, *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[bytes] | None:
        self.posts.append((ticket_id, body, cwd))
        if self.results:
            return self.results.pop(0)
        url = f"{URL_BASE}{len(self.posts)}"
        self.comments.append(_comment(url, self.store(body)))
        return _proc(0, url + "\n")

    def fetch(
        self, _ticket_id: str, _timeout: int, *, cwd: Path | None = None
    ) -> list[dict[str, object]] | None:
        del cwd
        self.fetches += 1
        if self.fetches <= self.hide_until_fetch:
            return []
        return list(self.comments)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


@pytest.fixture
def tracker(monkeypatch: pytest.MonkeyPatch) -> _FakeTracker:
    fake = _FakeTracker()
    monkeypatch.setattr("cw.tracker_post.post_issue_comment", fake.post)
    monkeypatch.setattr("cw.tracker_post.fetch_issue_comments", fake.fetch)
    monkeypatch.setattr("cw.tracker_post.time.sleep", fake.sleep)
    return fake


# ---------------------------------------------------------------------------
# split_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "limit"),
    [
        ("short\n", 10),
        ("a\nb\nc\n", 2),
        ("line one\nline two\nline three", 9),
        ("x" * 25, 10),
        ("head\n" + "y" * 23 + "\ntail", 10),
        ("crlf\r\nline\r\n" * 5, 7),
        ("no trailing newline", 4),
    ],
)
def test_split_text_keeps_every_character_in_order(text: str, limit: int) -> None:
    parts = tp.split_text(text, limit)
    assert "".join(parts) == text
    assert all(len(part) <= limit for part in parts)
    assert all(parts)


def test_split_text_breaks_on_line_boundaries() -> None:
    assert tp.split_text("aaa\nbbb\nccc\n", 8) == ["aaa\nbbb\n", "ccc\n"]


def test_split_text_splits_an_over_long_line_inside_the_line() -> None:
    assert tp.split_text("ab\n" + "z" * 7 + "\n", 3) == ["ab\n", "zzz", "zzz", "z\n"]


def test_split_text_never_separates_a_crlf_pair() -> None:
    parts = tp.split_text("abc\r\n", 4)
    assert parts == ["abc", "\r\n"]


def test_split_text_default_limit_is_the_part_text_size() -> None:
    text = ("w" * 999 + "\n") * 120
    parts = tp.split_text(text)
    assert len(parts) == len(text) // tp.PART_TEXT_CHARS + 1
    assert all(len(part) <= tp.PART_TEXT_CHARS for part in parts)
    assert (tp.PART_TEXT_CHARS, tp.MAX_COMMENT_CHARS) == (59_000, 60_000)


def test_split_text_returns_one_part_for_short_text() -> None:
    assert tp.split_text("one line") == ["one line"]


def test_split_text_rejects_a_non_positive_limit() -> None:
    with pytest.raises(ValueError, match="limit"):
        tp.split_text("x", 0)


# ---------------------------------------------------------------------------
# fence_for / wrap_part / strip_trailing_marker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "fence"),
    [
        ("plain", "```"),
        ("one ` tick", "```"),
        ("two `` ticks", "```"),
        ("three ``` ticks", "````"),
        ("five ````` ticks and ``` three", "``````"),
    ],
)
def test_fence_is_one_longer_than_the_longest_backtick_run(
    text: str, fence: str
) -> None:
    assert tp.fence_for(text) == fence


def test_wrap_part_header_blank_line_then_subheading() -> None:
    body = tp.wrap_part(
        "gate: x\n",
        index=1,
        total=1,
        header=FINALIZE_HEADER,
        subheading=FINALIZE_SUBHEADING,
        fence=True,
    )
    assert body == f"{FINALIZE_HEADER}\n\n{FINALIZE_SUBHEADING}\n\n```\ngate: x\n```"


def test_wrap_part_matches_the_impl_carve_out_shape() -> None:
    """The body starts with the header; the first non-blank line after it is
    the sub-heading (auto-dev-impl-appendix.md, Regress findings carve-out)."""
    body = tp.wrap_part(
        "MUST_FIX details",
        index=2,
        total=3,
        header=FINALIZE_HEADER,
        subheading=FINALIZE_SUBHEADING,
        fence=True,
    )
    lines = body.splitlines()
    assert lines[0] == FINALIZE_HEADER
    following = [line for line in lines[1:] if line.strip()]
    assert following[0] == FINALIZE_SUBHEADING


def test_wrap_part_labels_only_multi_part_posts() -> None:
    single = tp.wrap_part("t", index=1, total=1, header="## H", fence=False)
    multi = tp.wrap_part("t", index=2, total=3, header="## H", fence=False)
    assert "(part" not in single
    assert multi == "## H\n\n(part 2 of 3)\n\nt"


def test_wrap_part_unfenced_body_is_verbatim() -> None:
    assert tp.wrap_part("a\nb", index=1, total=1, header="## H") == "## H\n\na\nb"


def test_wrap_part_without_header_is_the_bare_body() -> None:
    assert tp.wrap_part("raw body\n", index=1, total=1) == "raw body\n"


def test_wrap_part_fenced_body_closes_on_its_own_line() -> None:
    body = tp.wrap_part("no newline", index=1, total=1, fence=True)
    assert body == "```\nno newline\n```"


def test_wrap_part_fence_outlasts_backticks_in_the_body() -> None:
    body = tp.wrap_part("has ``` inside", index=1, total=1, fence=True)
    assert body.startswith("````\n")
    assert body.endswith("\n````")


def test_strip_trailing_marker_removes_a_trailing_marker() -> None:
    assert tp.strip_trailing_marker(f"body\n\n{AGENT_COMMENT_MARKER}\n") == "body"


def test_strip_trailing_marker_is_idempotent() -> None:
    once = tp.strip_trailing_marker(
        f"body\n{AGENT_COMMENT_MARKER}\n\n{AGENT_COMMENT_MARKER}\n"
    )
    assert once == "body"
    assert tp.strip_trailing_marker(once) == once


def test_strip_trailing_marker_leaves_unmarked_text_untouched() -> None:
    text = f"mentions {AGENT_COMMENT_MARKER} mid-text\n\n"
    assert tp.strip_trailing_marker(text) == text


# ---------------------------------------------------------------------------
# plan_parts: redact, then measure, then shrink
# ---------------------------------------------------------------------------


def _plan(
    text: str,
    *,
    header: str | None = None,
    subheading: str | None = None,
    fence: bool = False,
    single: bool = False,
) -> tp.PartPlan:
    return tp.plan_parts(
        text,
        home=HOME,
        worktrees=[WORKTREE],
        header=header,
        subheading=subheading,
        fence=fence,
        single=single,
    )


def test_plan_parts_every_marked_part_fits_one_comment() -> None:
    text = ("finding line with some text\n" * 2) * 3000
    plan = _plan(
        text, header=FINALIZE_HEADER, subheading=FINALIZE_SUBHEADING, fence=True
    )
    assert len(plan.bodies) > 1
    assert plan.fits
    for index, body in enumerate(plan.bodies, start=1):
        assert len(agent_marked_body(body)) <= tp.MAX_COMMENT_CHARS
        assert f"(part {index} of {len(plan.bodies)})" in body


def test_plan_parts_short_text_is_one_unlabelled_part() -> None:
    plan = _plan("gate: x\n", header=FINALIZE_HEADER, fence=True)
    assert plan.bodies == (f"{FINALIZE_HEADER}\n\n```\ngate: x\n```",)
    assert not plan.redacted


def test_plan_parts_pathological_backticks_trigger_the_shrink_loop() -> None:
    text = "`" * 70_000
    plan = _plan(text, fence=True)
    assert all(len(agent_marked_body(b)) <= tp.MAX_COMMENT_CHARS for b in plan.bodies)
    assert sum(b.count("`") for b in plan.bodies) > len(text)


def test_plan_parts_wrapper_that_can_never_fit_raises() -> None:
    with pytest.raises(ValueError, match="wrapper"):
        _plan("body", header="#" * tp.MAX_COMMENT_CHARS)


def test_plan_parts_redacts_before_measuring() -> None:
    """Redaction that shrinks the text decides the part count, and only
    redacted text reaches a body."""
    long_path = f"{WORKTREE}/" + "d" * 80
    text = (long_path + "\n") * 1100  # 129,800 raw chars, 101,200 once redacted
    plan = _plan(text)
    assert plan.redacted
    assert len(tp.split_text(text)) == len(plan.bodies) + 1
    joined = "".join(plan.bodies)
    assert WORKTREE not in joined
    assert "/home/alice" not in joined
    assert "<worktree>/" in joined


def test_plan_parts_redaction_that_grows_the_text_is_measured_after() -> None:
    """A transcript path grows when redacted (21 to 35 characters per line):
    measuring the raw text would under-count; every body still fits."""
    raw_line = "~/.claude/projects/a\n"
    text = raw_line * 4_000  # 84,000 raw chars, 140,000 once redacted
    plan = _plan(text)
    assert plan.redacted
    assert all(len(agent_marked_body(b)) <= tp.MAX_COMMENT_CHARS for b in plan.bodies)
    redacted_line = "<home>/.claude/projects/<redacted>\n"
    assert len(plan.bodies) == len(tp.split_text(redacted_line * 4_000))
    assert len(plan.bodies) > len(tp.split_text(text))
    assert all("~/" not in b for b in plan.bodies)


def test_plan_parts_strips_a_trailing_marker_before_wrapping() -> None:
    plan = _plan(f"body\n\n{AGENT_COMMENT_MARKER}\n")
    assert plan.bodies == ("body",)
    assert not plan.redacted


def test_plan_parts_single_fits_or_reports_the_split_count() -> None:
    fits = _plan("short body", single=True)
    assert fits.fits
    assert fits.bodies == ("short body",)
    too_long = _plan("q" * 61_000, single=True)
    assert not too_long.fits
    assert len(too_long.bodies) > 1


def test_plan_parts_single_keeps_a_body_between_part_and_comment_size() -> None:
    """59,000 < len <= 60,000 - marker: one comment in single mode."""
    text = "s" * 59_500
    plan = _plan(text, single=True)
    assert plan.fits
    assert plan.bodies == (text,)


# ---------------------------------------------------------------------------
# Read-back helpers (ported from the deleted shell fence tests)
# ---------------------------------------------------------------------------


def test_readback_matching_body_is_ok() -> None:
    assert tp.readback_matches("## H\n\nbody", "## H\n\nbody")


def test_readback_normalises_crlf_and_trailing_whitespace() -> None:
    assert tp.readback_matches("## H\n\nbody\n", "## H\r\n\r\nbody  \r\n\n")


def test_readback_differing_body_is_a_mismatch() -> None:
    assert not tp.readback_matches("## H\n\nbody", "## H\n\nedited")


@pytest.mark.parametrize(
    ("sent", "fetched"), [("", ""), ("x", ""), ("", "x"), (" \n", "\r\n")]
)
def test_readback_empty_side_is_never_ok(sent: str, fetched: str) -> None:
    assert not tp.readback_matches(sent, fetched)


def test_normalize_for_compare_only_touches_crlf_and_the_tail() -> None:
    assert tp.normalize_for_compare("a\r\n  b \r\n\n") == "a\n  b"


def test_find_comment_body_matches_by_url_not_header() -> None:
    target = f"{URL_BASE}7"
    comments = [
        _comment(f"{URL_BASE}6", "## Blocking Review Findings\n\ndecoy"),
        _comment(target, "## Blocking Review Findings\n\nreal"),
        _comment(f"{URL_BASE}8", "## Blocking Review Findings\n\nnewer decoy"),
    ]
    assert (
        tp.find_comment_body(comments, target) == "## Blocking Review Findings\n\nreal"
    )


def test_find_comment_body_missing_comment_is_none() -> None:
    assert tp.find_comment_body(_observed_comments(), f"{URL_BASE}999") is None


def test_find_comment_body_ignores_a_non_string_body() -> None:
    entry = _comment(f"{URL_BASE}1", "x")
    entry["body"] = None
    assert tp.find_comment_body([entry], f"{URL_BASE}1") is None


@pytest.mark.parametrize(
    ("stdout", "url"),
    [
        (f"{URL_BASE}42\n", f"{URL_BASE}42"),
        (f"noise\n{URL_BASE}42\n".encode(), f"{URL_BASE}42"),
        ("", None),
        ("https://github.com/OWNER/REPO/issues/2810\n", None),
    ],
)
def test_parse_comment_url(stdout: str | bytes, url: str | None) -> None:
    assert tp.parse_comment_url(stdout) == url


# ---------------------------------------------------------------------------
# post_parts
# ---------------------------------------------------------------------------


def test_post_parts_success_returns_urls_oldest_first(tracker: _FakeTracker) -> None:
    result = tp.post_parts(TICKET, ("one", "two", "three"), cwd=Path("/repo"))
    assert result.status is tp.PostStatus.POSTED
    assert result.reason is None
    assert result.parts_total == len(tracker.posts)
    assert [(p.index, p.url) for p in result.posted] == [
        (1, f"{URL_BASE}1"),
        (2, f"{URL_BASE}2"),
        (3, f"{URL_BASE}3"),
    ]
    assert [body for _t, body, _c in tracker.posts] == ["one", "two", "three"]
    assert {cwd for _t, _b, cwd in tracker.posts} == {Path("/repo")}


def test_post_parts_first_post_failure_is_refused(tracker: _FakeTracker) -> None:
    tracker.results = [_proc(1)]
    result = tp.post_parts(TICKET, ("one", "two"), cwd=None)
    assert result.status is tp.PostStatus.REFUSED
    assert result.reason is tp.PostReason.POST_FAILED
    assert result.posted == ()
    assert len(tracker.posts) == 1


def test_post_parts_later_post_failure_is_partial(tracker: _FakeTracker) -> None:
    tracker.results = [_proc(0, f"{URL_BASE}1\n"), _proc(1)]
    tracker.comments.append(_comment(f"{URL_BASE}1", agent_marked_body("one")))
    result = tp.post_parts(TICKET, ("one", "two", "three"), cwd=None)
    assert result.status is tp.PostStatus.PARTIAL
    assert result.reason is tp.PostReason.POST_FAILED
    assert [p.url for p in result.posted] == [f"{URL_BASE}1"]
    assert [body for _t, body, _c in tracker.posts] == ["one", "two"]


def test_post_parts_unknown_outcome_is_partial_even_on_part_one(
    tracker: _FakeTracker,
) -> None:
    tracker.results = [None]
    result = tp.post_parts(TICKET, ("one",), cwd=None)
    assert result.status is tp.PostStatus.PARTIAL
    assert result.reason is tp.PostReason.POST_OUTCOME_UNKNOWN
    assert result.posted == ()


def test_post_parts_missing_url_is_partial(tracker: _FakeTracker) -> None:
    tracker.results = [_proc(0, "posted, no url\n")]
    result = tp.post_parts(TICKET, ("one", "two"), cwd=None)
    assert result.status is tp.PostStatus.PARTIAL
    assert result.reason is tp.PostReason.URL_MISSING
    assert result.posted == ()
    assert len(tracker.posts) == 1


def test_post_parts_readback_mismatch_stops_and_reports_the_url(
    tracker: _FakeTracker,
) -> None:
    tracker.store = lambda body: agent_marked_body(body + " (edited)")
    result = tp.post_parts(TICKET, ("one", "two"), cwd=None)
    assert result.status is tp.PostStatus.PARTIAL
    assert result.reason is tp.PostReason.READBACK_MISMATCH
    assert [p.url for p in result.posted] == [f"{URL_BASE}1"]
    assert len(tracker.posts) == 1
    assert tracker.sleeps == []  # a mismatch is not retried


def test_post_parts_readback_tolerates_tracker_normalisation(
    tracker: _FakeTracker,
) -> None:
    tracker.store = lambda body: agent_marked_body(body).replace("\n", "\r\n") + "  "
    result = tp.post_parts(TICKET, ("a\nb",), cwd=None)
    assert result.status is tp.PostStatus.POSTED


def test_post_parts_comment_never_found_is_readback_missing(
    tracker: _FakeTracker,
) -> None:
    tracker.hide_until_fetch = 99
    result = tp.post_parts(TICKET, ("one",), cwd=None)
    assert result.status is tp.PostStatus.PARTIAL
    assert result.reason is tp.PostReason.READBACK_MISSING
    assert [p.url for p in result.posted] == [f"{URL_BASE}1"]
    assert tracker.fetches == tp._READBACK_ATTEMPTS
    assert tracker.sleeps == [tp._READBACK_DELAY_SECONDS] * (tp._READBACK_ATTEMPTS - 1)


def test_post_parts_retries_a_not_yet_visible_comment(tracker: _FakeTracker) -> None:
    tracker.hide_until_fetch = 1
    result = tp.post_parts(TICKET, ("one",), cwd=None)
    assert result.status is tp.PostStatus.POSTED
    assert tracker.fetches == tracker.hide_until_fetch + 1
    assert tracker.sleeps == [tp._READBACK_DELAY_SECONDS]


def test_post_parts_failed_fetch_counts_as_not_found(
    tracker: _FakeTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cw.tracker_post.fetch_issue_comments", lambda *_a, **_k: None)
    result = tp.post_parts(TICKET, ("one",), cwd=None)
    assert result.reason is tp.PostReason.READBACK_MISSING


def test_post_parts_strips_a_leading_hash_from_the_ticket(
    tracker: _FakeTracker,
) -> None:
    tp.post_parts("#2810", ("one",), cwd=None)
    assert tracker.posts[0][0] == "2810"


def test_post_parts_posts_only_redacted_text_end_to_end(tracker: _FakeTracker) -> None:
    secret = "ghp_" + "x" * 36
    plan = tp.plan_parts(
        f"see {WORKTREE}/a.py and {secret}\n",
        home=HOME,
        worktrees=[WORKTREE],
        header=FINALIZE_HEADER,
        fence=True,
    )
    tp.post_parts(TICKET, plan.bodies, cwd=None)
    posted = tracker.posts[0][1]
    assert WORKTREE not in posted
    assert secret not in posted
    assert "<worktree>/a.py" in posted
    assert "<redacted>" in posted


# ---------------------------------------------------------------------------
# Closed enums (pinned to the result-JSON contract in the stage docs)
# ---------------------------------------------------------------------------


def test_post_status_values_are_closed() -> None:
    assert {s.value for s in tp.PostStatus} == {"posted", "refused", "partial"}


def test_post_reason_values_are_closed() -> None:
    assert {r.value for r in tp.PostReason} == {
        "body_unreadable",
        "empty_body",
        "tracker_not_github",
        "visibility_undetermined",
        "single_part_too_long",
        "post_failed",
        "post_outcome_unknown",
        "url_missing",
        "readback_mismatch",
        "readback_missing",
    }


# ---------------------------------------------------------------------------
# Machine-parsed bodies (plan Advisory Soundness Note 1)
# ---------------------------------------------------------------------------


def test_voided_findings_block_does_not_survive_redaction() -> None:
    """Why the voided-findings comment is allow-listed, not routed (#2810).

    A representative block whose evidence quotes a home path, a 40-char hash
    and a ``token:`` string must round-trip byte for byte and parse through the
    real ``_voided.py`` parser to be routable. It does not: the path is
    rewritten (the void's content fingerprint no longer matches) and the
    ``token:`` shape eats the closing quote, so the JSON no longer parses and
    the void is lost. If this test ever fails, re-evaluate routing that site.
    """
    entry = VoidedFinding(
        severity="MUST_FIX",
        file="src/cw/x.py",
        summary="quoted credential",
        evidence=(
            "open('/home/alice/src/x') # 0123456789abcdef0123456789abcdef01234567"
            " token: abc123"
        ),
        operator_comment_id="operator-login@2026-01-01T00:00:00Z",
    )
    block = render_voided_findings_block([entry])
    assert parse_voided_findings_block([block]) == [entry]

    plan = tp.plan_parts(block, home=HOME, worktrees=[], single=True)
    posted = plan.bodies[0]

    assert posted != block
    assert parse_voided_findings_block([posted]) != [entry]


def test_history_comment_shape_survives_when_it_carries_no_path_or_secret() -> None:
    """The convergence history comment is routed (lesser stakes than voided).

    Its ready-to-paste ``operator-settle`` payload is a convenience the
    operator can rebuild from the local ledger, and nothing in cw parses the
    comment back. A body with no path or secret shape round-trips unchanged,
    payload included; a path inside it is redacted like any finding text.
    """
    payload = json.dumps(
        {"identities": [{"file": "src/cw/x.py", "sha": "0123456789abcdef" * 2}]},
        indent=2,
    )
    body = (
        "## Review Convergence History\n\n"
        "| Round | Kind |\n|---|---|\n| 1 | full |\n\n"
        f"```json\n{payload}\n```\n\n{AGENT_COMMENT_MARKER}\n"
    )
    plan = tp.plan_parts(body, home=HOME, worktrees=[], single=True)
    assert plan.fits
    assert not plan.redacted
    assert agent_marked_body(plan.bodies[0]) == body.rstrip()
    fenced = plan.bodies[0].split("```json\n", 1)[1].split("\n```", 1)[0]
    assert json.loads(fenced) == json.loads(payload)

    leaked = tp.plan_parts(
        body.replace("src/cw/x.py", "/home/alice/x.py"),
        home=HOME,
        worktrees=[],
        single=True,
    )
    assert leaked.redacted
    assert "/home/alice" not in leaked.bodies[0]
