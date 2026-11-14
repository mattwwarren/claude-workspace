"""Tests for .claude/scripts/check_ticket_quotes.py (#1502).

The script proves that every quoted span a plan attributes to the ticket (title,
body, operator comments, binding resolutions) occurs verbatim, modulo a closed
normalization, in the live ticket corpus. It never fact-checks prose. Comments
the pipeline or an agent wrote are not quote sources: the script mirrors the
provenance predicates in ``cw`` (it must not import ``cw``) and a parity test
here pins each mirrored constant against its real home.

Loaded by path (it lives outside ``src/``). In-process tests drive ``evaluate``
and ``main`` for coverage; a few subprocess tests pin the real CLI.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests._shared.guard_fences import load_guard_script_module, run_guard_script_cli

if TYPE_CHECKING:
    import subprocess

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "check_ticket_quotes.py"
_FIXTURES = Path(__file__).parent / "fixtures" / "ticket_quotes"

_mod = load_guard_script_module(_SCRIPT, "check_ticket_quotes")

_FAB = "fabricated words that appear nowhere"


def _check(
    plan: str,
    *,
    body: str = "",
    title: str = "Ticket title here",
    comments: tuple[str, ...] = (),
    resolutions: str = "",
) -> dict[str, object]:
    data = {"title": title, "body": body, "comments": [{"body": c} for c in comments]}
    return _mod.evaluate(plan, _mod.build_corpus(data, resolutions))


def _entries(verdict: dict[str, object]) -> list[dict[str, object]]:
    unmatched = verdict["unmatched"]
    assert isinstance(unmatched, list)
    return unmatched


def _quotes(verdict: dict[str, object]) -> list[object]:
    return [entry["quote"] for entry in _entries(verdict)]


def _field(quote: str) -> str:
    return f'Ticket quote: "{quote}"\n'


def _cli(
    tmp_path: Path, plan: str, corpus: object, extra: list[str] | None = None
) -> tuple[Path, Path, list[str]]:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(plan, encoding="utf-8")
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")
    args = ["--plan", str(plan_path), "--corpus", str(corpus_path), *(extra or [])]
    return plan_path, corpus_path, args


# ---------------------------------------------------------------------------
# Acceptance fixtures (subprocess: the real CLI)
# ---------------------------------------------------------------------------


def _fixture_run(plan_name: str) -> subprocess.CompletedProcess[str]:
    return run_guard_script_cli(
        _SCRIPT,
        [
            "--plan",
            str(_FIXTURES / plan_name),
            "--corpus",
            str(_FIXTURES / "ticket_corpus.json"),
        ],
    )


def test_invented_quote_exits_1_with_exact_string() -> None:
    result = _fixture_run("plan_invented_quote.md")
    assert result.returncode == 1
    verdict = json.loads(result.stdout)
    assert verdict["quotes_checked"] == 2
    assert len(verdict["unmatched"]) == 1
    entry = _entries(verdict)[0]
    assert entry["quote"] == "The loop must never retry after two failures."
    assert entry["fragment"] == "The loop must never retry after two failures."
    assert entry["line"] == 9
    assert entry["trigger"] == "field"


def test_genuine_quotes_cover_all_four_triggers_and_exit_0() -> None:
    result = _fixture_run("plan_genuine_quotes.md")
    assert result.returncode == 0, result.stderr
    verdict = json.loads(result.stdout)
    assert verdict["unmatched"] == []
    assert verdict["quotes_checked"] == 5


def test_genuine_fixture_triggers_each_fire_once() -> None:
    plan = (_FIXTURES / "plan_genuine_quotes.md").read_text(encoding="utf-8")
    corpus = json.loads((_FIXTURES / "ticket_corpus.json").read_text(encoding="utf-8"))
    pieces = _mod.build_corpus(corpus, "")
    triggers = sorted(span.trigger for span in _mod.extract_spans(plan))
    assert triggers == [
        "attribution",
        "attribution",
        "blockquote",
        "field",
        "resolution-id",
    ]
    assert _mod.evaluate(plan, pieces)["unmatched"] == []


def test_agent_comment_in_fixture_is_not_a_source() -> None:
    plan = _field("the loop silently doubles the budget on resume")
    corpus = json.loads((_FIXTURES / "ticket_corpus.json").read_text(encoding="utf-8"))
    verdict = _mod.evaluate(plan, _mod.build_corpus(corpus, ""))
    assert _quotes(verdict) == ["the loop silently doubles the budget on resume"]


def test_main_module_guard_runs_as_script(tmp_path: Path) -> None:
    _, _, args = _cli(tmp_path, "no quotes here\n", {"title": "A title"})
    result = run_guard_script_cli(_SCRIPT, args)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"quotes_checked": 0, "unmatched": []}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("corpus_text", "plan_quote"),
    [
        ("the  quick brown\nfox   jumps high", "the quick brown fox jumps high"),
        ("The Quick Brown Fox", "the QUICK brown FOX"),
        ("do not retry\u2019s tail here", "do not retry's tail here"),
        ("it\u2019s fine to retry", "it's fine to retry"),
        ("retry — never forever", "retry - never forever"),
        ("retry \u2013 never forever", "retry - never forever"),
        ("retry - never forever", "retry — never forever"),
        ("**must** retry _twice_ only", "must retry twice only"),
        ("must `retry` twice only", "must retry twice only"),
        (
            "- first thing here\n- second thing here",
            "first thing here second thing here",
        ),
        (
            "1. first thing here\n2) second thing here",
            "first thing here second thing here",
        ),
        ("> quoted first line\n> quoted second", "quoted first line quoted second"),
        ("keep the cap configurable, but default", "keep the cap configurable."),
        ("keep the cap configurable; but", "keep the cap configurable;"),
        ("\uff21\uff22\uff23 and more words", "ABC and more words"),
    ],
)
def test_normalization_tolerates(corpus_text: str, plan_quote: str) -> None:
    verdict = _check(_field(plan_quote), body=corpus_text)
    assert verdict["unmatched"] == []
    assert verdict["quotes_checked"] == 1


def test_curly_quote_delimiters_are_accepted() -> None:
    plan = "Ticket quote: “keep the cap configurable”\n"
    verdict = _check(plan, body="keep the cap configurable, but default")
    assert verdict["unmatched"] == []
    assert verdict["quotes_checked"] == 1
    flagged = _check(plan, body="something else entirely")
    assert _quotes(flagged) == ["keep the cap configurable"]


def test_title_is_a_quote_source() -> None:
    verdict = _check(_field("Ticket title here"), title="Ticket title here")
    assert verdict["unmatched"] == []


def test_match_does_not_span_corpus_pieces() -> None:
    verdict = _check(
        _field("end of the body start of the comment"),
        body="end of the body",
        comments=("start of the comment",),
    )
    assert _quotes(verdict) == ["end of the body start of the comment"]


# ---------------------------------------------------------------------------
# Elision, minimum words, delimiters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sep", ["...", "…", "[...]", "[…]", "[editorial]"])
def test_elision_checks_each_fragment(sep: str) -> None:
    corpus = "alpha beta gamma and some filler delta epsilon zeta"
    plan = _field(f"alpha beta gamma {sep} delta epsilon zeta")
    assert _check(plan, body=corpus)["unmatched"] == []


def test_elision_flags_fabricated_fragment_and_reports_whole_quote() -> None:
    corpus = "alpha beta gamma and some filler"
    quote = "alpha beta gamma ... fabricated words here"
    verdict = _check(_field(quote), body=corpus)
    unmatched = verdict["unmatched"]
    assert isinstance(unmatched, list)
    assert len(unmatched) == 1
    assert unmatched[0]["quote"] == quote
    assert unmatched[0]["fragment"] == "fabricated words here"


def test_trailing_ellipsis_leaves_only_genuine_fragment() -> None:
    verdict = _check(_field("alpha beta gamma..."), body="alpha beta gamma delta")
    assert verdict["unmatched"] == []
    assert verdict["quotes_checked"] == 1


def test_fragment_of_two_words_is_skipped() -> None:
    verdict = _check(_field("two words"), body="nothing relevant")
    assert verdict == {"quotes_checked": 0, "unmatched": []}


def test_fragment_of_exactly_three_words_is_checked() -> None:
    verdict = _check(_field("three word quote"), body="nothing relevant")
    assert _quotes(verdict) == ["three word quote"]


def test_short_fragment_skipped_but_long_fragment_checked() -> None:
    quote = "tiny bit ... fabricated words here"
    verdict = _check(_field(quote), body="nothing relevant")
    assert _quotes(verdict) == [quote]
    assert _entries(verdict)[0]["fragment"] == "fabricated words here"


def test_single_quotes_are_never_delimiters() -> None:
    verdict = _check("Ticket quote: 'invented single quoted text here'\n")
    assert verdict == {"quotes_checked": 0, "unmatched": []}


def test_apostrophes_do_not_break_double_quoted_span() -> None:
    verdict = _check(_field("don't retry won't retry"), body="don't retry won't retry")
    assert verdict["unmatched"] == []


# ---------------------------------------------------------------------------
# Pre-processing: code, comments, line numbers, multi-line spans
# ---------------------------------------------------------------------------


def test_fenced_code_is_ignored_and_line_numbers_are_original() -> None:
    plan = (
        "intro\n"
        "```text\n"
        'Ticket quote: "fenced invented words here"\n'
        "```\n"
        "~~~\n"
        'Ticket quote: "tilde fenced invented words"\n'
        "~~~\n"
        f'Ticket quote: "{_FAB}"\n'
    )
    verdict = _check(plan)
    assert _quotes(verdict) == [_FAB]
    assert _entries(verdict)[0]["line"] == 8


def test_unclosed_fence_swallows_rest_of_plan() -> None:
    plan = f'```\nTicket quote: "{_FAB}"\n'
    assert _check(plan) == {"quotes_checked": 0, "unmatched": []}


def test_inline_code_quotes_are_ignored() -> None:
    plan = 'The ticket says `grep "inline invented words"` then.\n'
    assert _check(plan) == {"quotes_checked": 0, "unmatched": []}


def test_inline_code_inside_a_quote_does_not_false_positive() -> None:
    plan = 'Ticket quote: "visible counter on `cw status` shown"\n'
    verdict = _check(plan, body="a visible counter on cw status shown here")
    assert verdict["unmatched"] == []
    assert verdict["quotes_checked"] == 1


def test_html_comment_lines_are_ignored() -> None:
    plan = (
        f'<!-- Ticket quote: "{_FAB}" -->\n'
        "<!-- start\n"
        f'Ticket quote: "{_FAB} again"\n'
        "end -->\n"
        f'Ticket quote: "{_FAB} real"\n'
    )
    verdict = _check(plan)
    assert _quotes(verdict) == [f"{_FAB} real"]
    assert _entries(verdict)[0]["line"] == 5


def test_multiline_span_is_joined_and_checked() -> None:
    plan = 'Ticket quote: "alpha beta\ngamma delta\nepsilon zeta"\n'
    assert _check(plan, body="alpha beta gamma delta epsilon zeta")["unmatched"] == []
    flagged = _check(plan, body="something else")
    assert _quotes(flagged) == ["alpha beta gamma delta epsilon zeta"]


def test_unterminated_span_within_three_lines_is_closed() -> None:
    plan = 'Ticket quote: "one two\nthree\nfour\nfive six"\n'
    assert _quotes(_check(plan, body="x")) == ["one two three four five six"]


def test_unterminated_span_beyond_three_lines_is_skipped() -> None:
    plan = 'Ticket quote: "one two\nthree\nfour\nfive\nsix seven eight"\n'
    assert _check(plan, body="x") == {"quotes_checked": 0, "unmatched": []}


@pytest.mark.parametrize("stopper", ["", "- list item", "1. numbered", "## Heading"])
def test_multiline_span_stops_at_paragraph_end(stopper: str) -> None:
    plan = f'Ticket quote: "one two three\n{stopper}\nfour five six"\n'
    verdict = _check(plan, body="x")
    assert verdict == {"quotes_checked": 0, "unmatched": []}


# ---------------------------------------------------------------------------
# T1: labeled field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prefix",
    [
        "Ticket quote:",
        "Ticket evidence:",
        "Ticket text:",
        "Ticket excerpt:",
        "Ticket comment:",
        "Quote from the ticket:",
        "Quote from ticket:",
        "Operator quote:",
        "Resolution quote:",
        "AC4:",
        "AC 4:",
        "Acceptance criterion 2:",
        "TICKET QUOTE:",
        "- Ticket quote:",
        "* Ticket quote:",
        "1. Ticket quote:",
        "**Ticket quote:**",
        "**Ticket quote**:",
        "  - **Ticket quote:**",
    ],
)
def test_field_labels_are_checked(prefix: str) -> None:
    verdict = _check(f'{prefix} "{_FAB}"\n')
    assert _quotes(verdict) == [_FAB]
    assert _entries(verdict)[0]["trigger"] == "field"


def test_field_extracts_every_span_in_value() -> None:
    plan = 'Ticket quote: "genuine words from ticket" and "invented words here"\n'
    verdict = _check(plan, body="genuine words from ticket")
    assert _quotes(verdict) == ["invented words here"]
    assert verdict["quotes_checked"] == 2


@pytest.mark.parametrize(
    "line",
    [
        "Ticket quote: none",
        "Ticket quote: see the ticket body for the wording",
        "Ticket quote:",
        'Ticket note: "fabricated words that appear"',
        f'The label Ticket quote appears mid-line: "{_FAB}"',
    ],
)
def test_field_without_label_or_quote_yields_nothing(line: str) -> None:
    assert _check(line + "\n") == {"quotes_checked": 0, "unmatched": []}


# ---------------------------------------------------------------------------
# T2: attribution sentence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lead",
    [
        "The ticket says",
        "the issue description states",
        "The issue body reads",
        "The ticket title specified",
        "An acceptance criterion requires",
        "The acceptance criteria request",
        "AC3 specifies",
        "a ticket comment notes",
        "an issue comment asked",
        "The operator's decision reads",
        "The operator said",
        "The owner asked",
        "The reporter wrote",
        "The requester demands",
        "The pre-flight resolution mandates",
        "The resolution quoted",
        "R2 wrote",
        "comment 99 noted",
        "per the ticket:",
        "according to the operator,",
        "as the pre-flight resolution —",
        "per the acceptance criteria",
        "As the issue",
    ],
)
def test_attribution_forms_are_checked(lead: str) -> None:
    verdict = _check(f'{lead} "{_FAB}".\n')
    assert _quotes(verdict) == [_FAB]
    assert _entries(verdict)[0]["trigger"] == "attribution"


def test_attribution_gap_boundary_is_forty_characters() -> None:
    inside = "The ticket says " + "y" * 38 + f' "{_FAB}"\n'
    outside = "The ticket says " + "y" * 39 + f' "{_FAB}"\n'
    assert _quotes(_check(inside)) == [_FAB]
    assert _check(outside) == {"quotes_checked": 0, "unmatched": []}


@pytest.mark.parametrize(
    "lead",
    ["The docs say", "The reviewer states", "The planner wrote", "It reads", "Ticket"],
)
def test_attribution_to_unlisted_noun_is_ignored(lead: str) -> None:
    assert _check(f'{lead} "{_FAB}".\n') == {"quotes_checked": 0, "unmatched": []}


def test_attribution_does_not_cross_another_quote() -> None:
    plan = f'The ticket says "genuine words from ticket" then "{_FAB}" later.\n'
    verdict = _check(plan, body="genuine words from ticket")
    assert _quotes(verdict) == []
    assert verdict["quotes_checked"] == 1


def test_attribution_and_field_on_one_line_extract_span_once() -> None:
    plan = f'Ticket quote: the ticket says "{_FAB}"\n'
    verdict = _check(plan)
    assert _quotes(verdict) == [_FAB]
    assert verdict["quotes_checked"] == 1


# ---------------------------------------------------------------------------
# T3: identifier form
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lead",
    [
        "R1 —",
        "R12 -",
        "R3:",
        "R4 \u2013",
        "row:3 -",
        "row:12 —",
        "comment:abc123:",
        "comment:IC_kwDOabc —",
        "Decided: R2 —",
        "- Decided: row:1 —",
        "**Decided:** R2 —",
        "- R5 —",
    ],
)
def test_identifier_form_is_checked(lead: str) -> None:
    verdict = _check(f'{lead} "{_FAB}"\n')
    assert _quotes(verdict) == [_FAB]
    assert _entries(verdict)[0]["trigger"] == "resolution-id"


def test_identifier_form_matches_resolutions_text() -> None:
    plan = 'R1 — "use the lane config only"\n'
    assert _quotes(_check(plan)) == ["use the lane config only"]
    assert _check(plan, resolutions="R1 — use the lane config only.")["unmatched"] == []


def test_identifier_without_following_quote_yields_nothing() -> None:
    assert _check("R1 — use the lane config only\n") == {
        "quotes_checked": 0,
        "unmatched": [],
    }


# ---------------------------------------------------------------------------
# T4: blockquote
# ---------------------------------------------------------------------------


def test_blockquote_without_lead_in_is_ignored() -> None:
    plan = f"Some unrelated prose.\n\n> {_FAB}\n"
    assert _check(plan) == {"quotes_checked": 0, "unmatched": []}


def test_blockquote_at_top_of_plan_has_no_lead_in() -> None:
    assert _check(f"> {_FAB}\n") == {"quotes_checked": 0, "unmatched": []}


def test_blockquote_with_lead_in_is_checked() -> None:
    plan = f"The ticket says:\n\n> {_FAB}\n> and more words\n"
    verdict = _check(plan)
    assert _quotes(verdict) == [f"{_FAB} and more words"]
    entry = _entries(verdict)[0]
    assert entry["trigger"] == "blockquote"
    assert entry["line"] == 3


def test_blockquote_genuine_passes() -> None:
    plan = "Per the issue body:\n> keep the cap\n> configurable here\n"
    assert _check(plan, body="Keep the cap configurable here")["unmatched"] == []


def test_blockquote_lead_in_must_be_nearest_non_blank_line() -> None:
    plan = f"The ticket says:\nUnrelated line in between.\n\n> {_FAB}\n"
    assert _check(plan) == {"quotes_checked": 0, "unmatched": []}


def test_blockquote_lead_in_without_article_counts() -> None:
    plan = f"Ticket comment 4:\n> {_FAB}\n"
    assert _quotes(_check(plan)) == [_FAB]


def test_blockquote_lead_in_is_never_another_blockquote() -> None:
    plan = f"The ticket says:\n\n> the ticket describes a genuine thing\n\n> {_FAB}\n"
    verdict = _check(plan, body="The ticket describes a genuine thing")
    assert verdict == {"quotes_checked": 1, "unmatched": []}


def test_blockquote_run_ends_at_first_non_quote_line() -> None:
    plan = f"The ticket says:\n> {_FAB}\nnot quoted text here\n"
    assert _quotes(_check(plan)) == [_FAB]


# ---------------------------------------------------------------------------
# Comment corpus and provenance
# ---------------------------------------------------------------------------

_QUOTE = "keep the cap configurable please"


def test_quote_in_body_matches() -> None:
    assert _check(_field(_QUOTE), body=f"Body says {_QUOTE} ok")["unmatched"] == []


def test_quote_in_operator_comment_matches() -> None:
    verdict = _check(_field(_QUOTE), comments=(f"Operator: {_QUOTE}.",))
    assert verdict["unmatched"] == []


def test_quote_only_in_agent_marked_comment_is_flagged() -> None:
    comment = f"Analysis: {_QUOTE}\n\n<!-- cw-agent-authored -->"
    assert _quotes(_check(_field(_QUOTE), comments=(comment,))) == [_QUOTE]


def test_agent_marker_mid_body_is_dropped() -> None:
    comment = f"<!-- cw-agent-authored -->\nSomething {_QUOTE} here"
    assert _quotes(_check(_field(_QUOTE), comments=(comment,))) == [_QUOTE]


@pytest.mark.parametrize(
    "disclosure",
    [
        "Relayed by an agent",
        "authored by the orchestrator",
        "drafted by Claude",
        "relayed on behalf of the operator",
        "on behalf of the operator",
        "posted under the operator's own account",
        "relaying the operator's words",
        "WRITTEN BY AN ASSISTANT",
    ],
)
def test_self_disclosing_comment_is_flagged(disclosure: str) -> None:
    comment = f"{disclosure}: {_QUOTE}"
    assert _quotes(_check(_field(_QUOTE), comments=(comment,))) == [_QUOTE]


def test_self_disclosure_mid_body_is_found_by_search() -> None:
    comment = f"{_QUOTE}\n\nThis was posted under the operator's own login."
    assert _quotes(_check(_field(_QUOTE), comments=(comment,))) == [_QUOTE]


@pytest.mark.parametrize(
    "header",
    [
        "## Multi-Marker Gate Blocked",
        "## Pending Verification Scan",
        "## Blocking Review Findings",
        "## Operator-Actionable Review Findings",
    ],
)
def test_pipeline_fixed_header_comment_is_flagged(header: str) -> None:
    comment = f"{header}\n\n{_QUOTE}"
    assert _quotes(_check(_field(_QUOTE), comments=(comment,))) == [_QUOTE]


@pytest.mark.parametrize("lead", [" ", "\n\n", "  \n \t"])
def test_fixed_header_after_leading_whitespace_is_still_dropped(lead: str) -> None:
    comment = f"{lead}## Blocking Review Findings\n\n{_QUOTE}"
    assert _quotes(_check(_field(_QUOTE), comments=(comment,))) == [_QUOTE]


def test_fixed_header_quoted_mid_body_is_not_dropped() -> None:
    comment = f"I saw this earlier:\n## Blocking Review Findings\n{_QUOTE}"
    assert _check(_field(_QUOTE), comments=(comment,))["unmatched"] == []


@pytest.mark.parametrize(
    "marker",
    [
        "<!-- plan-spec-reviewed: 2026-11-14 v2 -->",
        "<!-- plan-soundness-reviewed v1 -->",
    ],
)
def test_signoff_marker_comment_is_flagged(marker: str) -> None:
    comment = f"{_QUOTE}\n\n{marker}"
    assert _quotes(_check(_field(_QUOTE), comments=(comment,))) == [_QUOTE]


@pytest.mark.parametrize(
    "marker", ["<!-- plan-spec-reviewed", "<!-- plan-soundness-reviewed"]
)
def test_signoff_marker_mid_body_is_dropped_by_substring(marker: str) -> None:
    comment = f"Intro text first.\n{marker}\n{_QUOTE}"
    assert _quotes(_check(_field(_QUOTE), comments=(comment,))) == [_QUOTE]


def test_clean_comment_alongside_dropped_ones_still_matches() -> None:
    dropped = f"{_QUOTE}\n<!-- cw-agent-authored -->"
    verdict = _check(_field(_QUOTE), comments=(dropped, f"Operator: {_QUOTE}"))
    assert verdict["unmatched"] == []


def test_resolutions_are_never_provenance_filtered() -> None:
    resolutions = f"relayed by an agent: {_QUOTE}"
    assert _check(_field(_QUOTE), resolutions=resolutions)["unmatched"] == []


def test_provenance_ok_direct() -> None:
    assert _mod._provenance_ok("plain operator words") is True
    assert _mod._provenance_ok("<!-- cw-agent-authored -->") is False


# ---------------------------------------------------------------------------
# Output shape and dedupe
# ---------------------------------------------------------------------------


def test_identical_quotes_are_deduped() -> None:
    plan = _field(_FAB) + f'The ticket says "{_FAB}".\n' + f'R1 — "{_FAB}"\n'
    verdict = _check(plan)
    assert _quotes(verdict) == [_FAB]
    assert verdict["quotes_checked"] == 1
    assert _entries(verdict)[0]["line"] == 1


def test_unmatched_entries_are_in_plan_order() -> None:
    plan = (
        f'R1 — "{_FAB} one"\n'
        f'Ticket quote: "{_FAB} two"\n'
        f'The ticket says "{_FAB} three".\n'
    )
    verdict = _check(plan)
    assert [e["line"] for e in _entries(verdict)] == [1, 2, 3]


def test_plan_without_quotes_is_clean() -> None:
    assert _check("# Plan\n\nNothing quoted here.\n") == {
        "quotes_checked": 0,
        "unmatched": [],
    }


# ---------------------------------------------------------------------------
# build_corpus validation
# ---------------------------------------------------------------------------


def test_linear_shape_uses_description_for_body() -> None:
    data = {"title": "T", "description": "the real description words", "comments": []}
    pieces = _mod.build_corpus(data, "")
    assert (
        _mod.evaluate(_field("the real description words"), pieces)["unmatched"] == []
    )


@pytest.mark.parametrize(
    "data",
    [
        [],
        "text",
        {"title": 5, "body": "b"},
        {"title": "t", "body": 5},
        {"title": "t", "description": 5},
        {"title": "t", "comments": "nope"},
        {"title": "t", "comments": {}},
        {"title": "t", "comments": ["nope"]},
        {"title": "t", "comments": [{"body": 5}]},
        {},
        {"title": "", "body": "  ", "comments": [{"body": " "}]},
        {"title": None, "body": None, "comments": []},
    ],
)
def test_build_corpus_rejects_bad_or_empty_shapes(data: object) -> None:
    with pytest.raises(_mod.CorpusError):
        _mod.build_corpus(data, "")


def test_comment_without_body_key_is_ignored() -> None:
    data = {"title": "T", "comments": [{"id": "x"}]}
    assert _mod.build_corpus(data, "") == ["t"]


# ---------------------------------------------------------------------------
# main(): exit codes (in-process, for coverage)
# ---------------------------------------------------------------------------


def test_main_exit_0_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, args = _cli(
        tmp_path, _field("the body words here"), {"body": "the body words here"}
    )
    assert _mod.main(args) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"quotes_checked": 1, "unmatched": []}


def test_main_exit_1_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, args = _cli(tmp_path, _field(_FAB), {"body": "something else"})
    assert _mod.main(args) == 1
    verdict = json.loads(capsys.readouterr().out)
    assert _entries(verdict)[0] == {
        "quote": _FAB,
        "fragment": _FAB,
        "line": 1,
        "trigger": "field",
    }


def test_main_resolutions_file_is_a_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    res = tmp_path / "res.txt"
    res.write_text("R1 — use the lane config only", encoding="utf-8")
    _, _, args = _cli(
        tmp_path,
        'R1 — "use the lane config only"\n',
        {"title": "A title"},
        ["--resolutions", str(res)],
    )
    assert _mod.main(args) == 0
    assert json.loads(capsys.readouterr().out)["quotes_checked"] == 1


def _assert_exit_2(rc: int, capsys: pytest.CaptureFixture[str]) -> None:
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert captured.err.startswith("check_ticket_quotes:")


def test_main_unreadable_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, corpus, _ = _cli(tmp_path, "x", {"title": "t"})
    rc = _mod.main(["--plan", str(tmp_path / "absent.md"), "--corpus", str(corpus)])
    _assert_exit_2(rc, capsys)


def test_main_non_utf8_plan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan, _, args = _cli(tmp_path, "x", {"title": "t"})
    plan.write_bytes(b"\xff\xfe\xfa bad")
    _assert_exit_2(_mod.main(args), capsys)


def test_main_unreadable_corpus(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan, _, _ = _cli(tmp_path, "x", {"title": "t"})
    rc = _mod.main(["--plan", str(plan), "--corpus", str(tmp_path / "absent.json")])
    _assert_exit_2(rc, capsys)


def test_main_bad_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, corpus, args = _cli(tmp_path, "x", {"title": "t"})
    corpus.write_text("{not json", encoding="utf-8")
    _assert_exit_2(_mod.main(args), capsys)


@pytest.mark.parametrize(
    "data",
    [[], {"title": 5}, {"comments": "x", "title": "t"}, {}, {"title": " ", "body": ""}],
)
def test_main_wrong_types_or_empty_corpus(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], data: object
) -> None:
    _, _, args = _cli(tmp_path, "x", data)
    _assert_exit_2(_mod.main(args), capsys)


def test_main_unreadable_resolutions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, args = _cli(
        tmp_path, "x", {"title": "t"}, ["--resolutions", str(tmp_path / "absent.txt")]
    )
    _assert_exit_2(_mod.main(args), capsys)


def test_main_missing_plan_option_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = tmp_path / "c.json"
    corpus.write_text('{"title": "t"}', encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        _mod.main(["--corpus", str(corpus)])
    assert excinfo.value.code == 2
    assert capsys.readouterr().out == ""


def test_cli_exit_2_has_no_stdout_json(tmp_path: Path) -> None:
    result = run_guard_script_cli(
        _SCRIPT, ["--plan", str(tmp_path / "nope.md"), "--corpus", str(tmp_path / "x")]
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr


# ---------------------------------------------------------------------------
# Header, imports, parity with the real constants
# ---------------------------------------------------------------------------


def test_script_version_header_is_line_two() -> None:
    lines = _SCRIPT.read_text(encoding="utf-8").splitlines()
    assert lines[1] == "# cw-script-version: 1"


def test_script_imports_no_cw_and_launches_nothing() -> None:
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "cw" not in imported
    assert not imported & {"subprocess", "os", "shutil"}


def test_provenance_constants_match_their_real_homes() -> None:
    from cw.codex_fix_loop.constraints import _FIXED_HEADERS
    from cw.dev_queue.lifecycle import _PLAN_SOUNDNESS_MARKER
    from cw.gh.issues import (
        _PLAN_MARKER,
        _SELF_DISCLOSURE_RE,
        AGENT_COMMENT_MARKER,
    )

    assert _mod._AGENT_COMMENT_MARKER == AGENT_COMMENT_MARKER
    assert _mod._SELF_DISCLOSURE_RE.pattern == _SELF_DISCLOSURE_RE.pattern
    assert _mod._SELF_DISCLOSURE_RE.flags == _SELF_DISCLOSURE_RE.flags
    assert _mod._FIXED_HEADERS == _FIXED_HEADERS
    assert _mod._PLAN_SPEC_MARKER == _PLAN_MARKER
    assert _mod._PLAN_SOUNDNESS_MARKER == _PLAN_SOUNDNESS_MARKER
