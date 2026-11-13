"""Tests for .claude/scripts/check_premise_evidence.py (#2343).

The script validates the structure and dispatch time window of the
``Observed-at`` / ``Kind`` / ``Tool-call`` stamps on Product Manager Reviewer
premises (``scan-block``) and on a plan's ``## Self-Verified Premises`` entries
(``draft``). It proves freshness shape only; it never reads a session
transcript and never matches ``Tool-call`` against anything.

Loaded by path (it lives outside ``src/`` and must not import ``cw``). In-process
tests freeze the module's ``_utcnow()`` clock; the subprocess CLI tests build
timestamps relative to the real clock.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests._shared.guard_fences import load_guard_script_module, run_guard_script_cli

if TYPE_CHECKING:
    import subprocess

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "check_premise_evidence.py"
_FIXTURES = Path(__file__).parent / "fixtures" / "premise_evidence"

_mod = load_guard_script_module(_SCRIPT, "check_premise_evidence")
_REAL_UTCNOW = _mod._utcnow

_NOW = datetime(2026, 11, 13, 5, 0, 0, tzinfo=UTC)
_STAGE_STARTED = "2026-11-13T04:52:06.649980+00:00"
_WINDOW_START = datetime(2026, 11, 13, 4, 52, 6, tzinfo=UTC)
_IN = "2026-11-13T04:55:00Z"
_BEFORE = "2026-11-13T04:00:00Z"
_FUTURE = "2026-11-13T05:10:00Z"
_EM = " — "

_REASON_CODES = {
    "observed-at-missing",
    "observed-at-malformed",
    "observed-before-window",
    "observed-in-future",
    "route-missing-or-invalid",
    "kind-missing-or-invalid",
    "tool-call-missing",
    "tool-call-malformed",
    "observed-attempt-invalid",
    "duplicate-key",
    "context-unavailable",
    "legacy-no-metadata",
    "relabeled-without-fresh-check",
    "unparseable-legacy",
}


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_mod, "_utcnow", lambda: _NOW)


# ---------------------------------------------------------------------------
# Builders (module-private; File A is their only consumer)
# ---------------------------------------------------------------------------


def _pm_item(
    claim: str = "the API paginates results",
    *,
    verified: str = "YES",
    overrides: dict[str, str | None] | None = None,
) -> str:
    """One PM premises-block item body (without its leading number)."""
    fields: dict[str, str | None] = {
        "Plan depends on it for": "the loop shape",
        "Evidence in plan or ticket": "none",
        "Verify before building by": "curl it",
        "Impact": "CODE-AFFECTING",
        "Verified": verified,
        "Citation": "docs quote",
        "Observed-at": _IN,
        "Kind": "LIVE",
        "Tool-call": "Bash: curl -s https://example.test/api",
    }
    fields.update(overrides or {})
    lines = [claim]
    lines.extend(
        f"   - {key}: {value}" for key, value in fields.items() if value is not None
    )
    return "\n".join(lines)


def _block(*items: str) -> str:
    header = f"PREMISES TO VERIFY — {len(items)} items\n\n"
    body = "\n\n".join(f"{n}. {item}" for n, item in enumerate(items, 1))
    return f"{header}{body}\n"


def _new_entry(
    claim: str = "the CLI exposes --json",
    *,
    route: str | None = "VERIFIED",
    observed_at: str | None = _IN,
    kind: str | None = "LIVE",
    tool_call: str | None = "Bash: tool --help",
    attempt: str | None = None,
) -> str:
    parts = [f"- {claim}", "Evidence: `tool --help` shows --json", "Depends on: parser"]
    for key, value in (
        ("Route", route),
        ("Observed-at", observed_at),
        ("Kind", kind),
        ("Tool-call", tool_call),
        ("Observed-attempt", attempt),
    ):
        if value is not None:
            parts.append(f"{key}: {value}")
    return _EM.join(parts)


def _draft(*entries: str) -> str:
    body = "\n".join(entries)
    return (
        "# Plan\n\n## Patterns Found\n\n- thing\n\n"
        f"## Self-Verified Premises\n\n{body}\n\n"
        "## Deferred Premises\n\n- other claim — Evidence: x — Depends on: y\n"
    )


def _legacy_block(
    claim: str, evidence: str, depends: str = "Plan depends on this for: stuff"
) -> str:
    return f"- Claim: {claim}\n  Evidence: {evidence}\n  {depends}"


def _one(result: list[dict[str, object]]) -> dict[str, object]:
    assert len(result) == 1
    return result[0]


def _scan(
    text: str, window: datetime | None = _WINDOW_START
) -> list[dict[str, object]]:
    return list(_mod.evaluate_scan_block(text, window))


def _draft_eval(
    text: str, window: datetime | None = _WINDOW_START
) -> list[dict[str, object]]:
    return list(_mod.evaluate_draft(text, window))


def _reasons(item: dict[str, object]) -> list[str]:
    reasons = item["reasons"]
    assert isinstance(reasons, list)
    return [str(reason) for reason in reasons]


def _items(payload: dict[str, object] | None) -> list[dict[str, object]]:
    assert payload is not None
    items = payload["items"]
    assert isinstance(items, list)
    return items


def _scan_one(
    overrides: dict[str, str | None], window: datetime | None = _WINDOW_START
) -> dict[str, object]:
    return _one(_scan(_block(_pm_item(overrides=overrides)), window))


# ---------------------------------------------------------------------------
# Static guards: stdlib only, no transcript access
# ---------------------------------------------------------------------------


def test_script_version_marker_is_line_two() -> None:
    lines = _SCRIPT.read_text().splitlines()
    assert lines[1] == "# cw-script-version: 1"


def test_script_has_no_cw_import() -> None:
    tree = ast.parse(_SCRIPT.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] != "cw" for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "cw"


def test_script_does_not_touch_transcripts() -> None:
    source = _SCRIPT.read_text()
    assert ".jsonl" not in source
    assert "projects" not in source
    assert "Path.home" not in source


def test_real_clock_is_timezone_aware_utc() -> None:
    before = datetime.now(UTC)
    observed = _REAL_UTCNOW()
    assert observed.tzinfo is not None
    assert observed.utcoffset() == timedelta(0)
    assert before <= observed <= datetime.now(UTC)


# ---------------------------------------------------------------------------
# Timestamp and context parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "2026-11-13T04:52:06",
        "2026-11-13T04:52:06.649980",
        "2026-11-13T04:52:06.649980+00:00",
        "2026-11-13T04:52:06Z",
        "2026-11-13T06:52:06+02:00",
    ],
)
def test_stage_started_at_parses_leniently(raw: str) -> None:
    parsed = _mod._parse_context_timestamp(raw)
    assert parsed.replace(microsecond=0) == _WINDOW_START


def test_stage_started_at_garbage_is_none() -> None:
    assert _mod._parse_context_timestamp("yesterday") is None


def _write_context(tmp_path: Path, payload: str) -> Path:
    path = tmp_path / "cw-context.json"
    path.write_text(payload)
    return path


def test_window_start_floors_to_the_second(tmp_path: Path) -> None:
    ctx = _write_context(tmp_path, json.dumps({"stage_started_at": _STAGE_STARTED}))
    assert _mod._load_window_start(ctx) == _WINDOW_START


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        "[1, 2]",
        "{}",
        '{"stage_started_at": 5}',
        '{"stage_started_at": "never"}',
    ],
)
def test_window_start_unavailable_payloads(tmp_path: Path, payload: str) -> None:
    assert _mod._load_window_start(_write_context(tmp_path, payload)) is None


def test_window_start_missing_file_is_none(tmp_path: Path) -> None:
    assert _mod._load_window_start(tmp_path / "absent.json") is None


def test_window_start_undecodable_file_is_none(tmp_path: Path) -> None:
    path = tmp_path / "cw-context.json"
    path.write_bytes(b"\xff\xfe\x00bad")
    assert _mod._load_window_start(path) is None


# ---------------------------------------------------------------------------
# scan-block: block parser
# ---------------------------------------------------------------------------


def test_block_parser_items_subbullets_and_continuations() -> None:
    text = (
        "PREMISES TO VERIFY — 2 items\n\n"
        "1. first claim\n"
        "   - Verified: YES\n"
        "   - Citation: line one\n"
        "     line two of the citation\n"
        "   - Mystery-Key: ignored\n"
        "   - Observed-at: 2026-11-13T04:55:00Z\n"
        "\n"
        "2. second claim\n"
        "   - Verified: NO\n"
    )
    items = _mod._parse_block_items(text)
    assert [i.claim for i in items] == ["first claim", "second claim"]
    first = items[0].fields
    assert first["verified"] == "YES"
    assert first["citation"] == "line one line two of the citation"
    assert first["observed-at"] == "2026-11-13T04:55:00Z"
    assert "mystery-key" not in first
    assert items[1].fields == {"verified": "NO"}


def test_block_parser_indented_numbered_citation_is_continuation() -> None:
    text = (
        "1. first claim\n"
        "   - Verified: YES\n"
        "   - Citation: line one\n"
        "     2. numbered citation output\n"
    )
    items = _mod._parse_block_items(text)
    assert len(items) == 1
    assert items[0].fields["citation"] == "line one 2. numbered citation output"


def test_block_parser_ignores_text_before_first_item() -> None:
    assert _mod._parse_block_items("header only\nno items here\n") == []


def test_block_parser_first_duplicate_key_wins() -> None:
    text = "1. c\n   - Verified: YES\n   - Verified: NO\n"
    assert _mod._parse_block_items(text)[0].fields["verified"] == "YES"


# ---------------------------------------------------------------------------
# scan-block: verdicts
# ---------------------------------------------------------------------------


def test_scan_bound_happy_path() -> None:
    item = _scan_one({})
    assert item == {
        "claim": "the API paginates results",
        "verdict": "BOUND",
        "reasons": [],
    }


def test_scan_only_yes_items_get_verdicts() -> None:
    text = _block(
        _pm_item("yes claim"),
        _pm_item("no claim", verified="NO"),
        _pm_item("defer claim", verified="DEFER"),
        _pm_item("lower claim", verified="yes"),
    )
    assert [i["claim"] for i in _scan(text)] == ["yes claim"]


def test_scan_missing_verified_is_not_evaluated() -> None:
    assert _scan(_block(_pm_item(overrides={"Verified": None}))) == []


def test_scan_blank_verified_is_not_evaluated() -> None:
    assert _scan(_block(_pm_item(verified=""))) == []


def test_scan_window_start_is_inclusive() -> None:
    assert _scan_one({"Observed-at": "2026-11-13T04:52:06Z"})["verdict"] == "BOUND"


def test_scan_one_second_before_window_is_unbound() -> None:
    item = _scan_one({"Observed-at": "2026-11-13T04:52:05Z"})
    assert item["verdict"] == "UNBOUND"
    assert item["reasons"] == ["observed-before-window"]


def test_scan_upper_bound_is_now_plus_skew() -> None:
    edge = (_NOW + timedelta(seconds=_mod._CLOCK_SKEW_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert _scan_one({"Observed-at": edge})["verdict"] == "BOUND"


def test_scan_reason_observed_at_missing() -> None:
    item = _scan_one({"Observed-at": None})
    assert item["verdict"] == "UNBOUND"
    assert item["reasons"] == ["observed-at-missing"]


def test_scan_reason_observed_at_empty_is_missing() -> None:
    assert _scan_one({"Observed-at": ""})["reasons"] == ["observed-at-missing"]


@pytest.mark.parametrize(
    "value",
    [
        "2026-11-13T04:55:00",
        "2026-11-13T04:55:00+00:00",
        "2026-11-13T04:55:00.123Z",
        "2026-11-13T4:55:00Z",
        "2026-13-45T04:55:00Z",
        "yesterday",
    ],
)
def test_scan_reason_observed_at_malformed(value: str) -> None:
    item = _scan_one({"Observed-at": value})
    assert item["verdict"] == "UNBOUND"
    assert item["reasons"] == ["observed-at-malformed"]


def test_scan_reason_observed_in_future() -> None:
    item = _scan_one({"Observed-at": _FUTURE})
    assert item["verdict"] == "UNBOUND"
    assert item["reasons"] == ["observed-in-future"]


@pytest.mark.parametrize("kind", [None, "", "DYNAMIC", "static"])
def test_scan_reason_kind_missing_or_invalid(kind: str | None) -> None:
    item = _scan_one({"Kind": kind})
    assert item["verdict"] == "UNBOUND"
    assert item["reasons"] == ["kind-missing-or-invalid"]


@pytest.mark.parametrize("kind", ["STATIC", "LIVE"])
def test_scan_valid_kinds_bind(kind: str) -> None:
    assert _scan_one({"Kind": kind})["verdict"] == "BOUND"


@pytest.mark.parametrize("value", [None, ""])
def test_scan_reason_tool_call_missing(value: str | None) -> None:
    item = _scan_one({"Tool-call": value})
    assert item["verdict"] == "UNBOUND"
    assert item["reasons"] == ["tool-call-missing"]


@pytest.mark.parametrize(
    "value",
    ["curl https://example.test", "Bash:", "Bash:   ", "bad name: x", "1Bash: x"],
)
def test_scan_reason_tool_call_malformed(value: str) -> None:
    item = _scan_one({"Tool-call": value})
    assert item["verdict"] == "UNBOUND"
    assert item["reasons"] == ["tool-call-malformed"]


@pytest.mark.parametrize(
    "value", ["Bash: ls", "WebFetch: https://x.test/y", "mcp__srv__tool: {}"]
)
def test_scan_tool_call_shapes_bind(value: str) -> None:
    assert _scan_one({"Tool-call": value})["verdict"] == "BOUND"


@pytest.mark.parametrize("value", ["-1", "abc", "1.5", "", "٣"])
def test_scan_reason_observed_attempt_invalid(value: str) -> None:
    item = _scan_one({"Observed-attempt": value})
    assert item["verdict"] == "UNBOUND"
    assert item["reasons"] == ["observed-attempt-invalid"]


@pytest.mark.parametrize("value", ["0", "3", "12"])
def test_scan_observed_attempt_valid_binds(value: str) -> None:
    assert _scan_one({"Observed-attempt": value})["verdict"] == "BOUND"


def test_scan_reason_context_unavailable() -> None:
    item = _scan_one({}, window=None)
    assert item["verdict"] == "UNBOUND"
    assert item["reasons"] == ["context-unavailable"]


def test_scan_context_unavailable_accompanies_structural_defects() -> None:
    item = _scan_one({"Kind": None, "Tool-call": None}, window=None)
    assert item["reasons"] == [
        "kind-missing-or-invalid",
        "tool-call-missing",
        "context-unavailable",
    ]


def test_scan_multiple_reasons_in_field_order() -> None:
    item = _scan_one(
        {
            "Observed-at": _BEFORE,
            "Kind": None,
            "Tool-call": "nope",
            "Observed-attempt": "x",
        }
    )
    assert item["reasons"] == [
        "observed-before-window",
        "kind-missing-or-invalid",
        "tool-call-malformed",
        "observed-attempt-invalid",
    ]


def test_scan_emits_only_bound_or_unbound_and_known_codes() -> None:
    text = _block(
        _pm_item("a"),
        _pm_item("b", overrides={"Observed-at": None}),
        _pm_item("c", overrides={"Kind": "x", "Observed-at": _FUTURE}),
    )
    result = _scan(text)
    assert {i["verdict"] for i in result} <= {"BOUND", "UNBOUND"}
    for item in result:
        assert set(_reasons(item)) <= _REASON_CODES


# ---------------------------------------------------------------------------
# draft: section and entry splitting
# ---------------------------------------------------------------------------


def test_draft_without_section_is_empty() -> None:
    assert _draft_eval("# Plan\n\n## Patterns Found\n\n- x\n") == []


def test_draft_section_stops_at_next_h2() -> None:
    result = _draft_eval(_draft(_new_entry("only claim")))
    assert [i["claim"] for i in result] == ["only claim"]


def test_draft_text_before_first_entry_is_ignored() -> None:
    text = "## Self-Verified Premises\n\nIntro prose.\n\n- a claim—\n"
    entries = _mod._split_entries(text)
    assert len(entries) == 1
    assert entries[0][0].startswith("- a claim")


def test_draft_split_entries_collects_continuations() -> None:
    text = (
        "## Self-Verified Premises\n\n"
        "- Claim: x\n  Evidence: y\n  Plan depends on this for: z\n"
        "- second one\n"
        "## Next\n- not mine\n"
    )
    entries = _mod._split_entries(text)
    assert len(entries) == 2
    assert entries[0] == [
        "- Claim: x",
        "  Evidence: y",
        "  Plan depends on this for: z",
    ]
    assert entries[1] == ["- second one"]


def test_draft_split_entries_trims_trailing_blank_lines() -> None:
    text = "## Self-Verified Premises\n\n- one\n  more\n\n\n- two\n\n"
    assert _mod._split_entries(text) == [["- one", "  more"], ["- two"]]


def test_draft_split_entries_no_heading() -> None:
    assert _mod._split_entries("- stray\n") == []


# ---------------------------------------------------------------------------
# draft: parse order (R8)
# ---------------------------------------------------------------------------


def test_parse_form_a_this_for() -> None:
    entry = _mod._parse_entry(
        [
            "- Claim: it works",
            "  Evidence: docs quote",
            "  Plan depends on this for: loop",
        ]
    )
    assert entry.form == "a"
    assert entry.claim == "it works"
    assert entry.evidence == "docs quote"
    assert entry.depends_on == "loop"


def test_parse_form_a_it_for() -> None:
    entry = _mod._parse_entry(
        [
            "- Claim: it works",
            "  Evidence: docs quote",
            "  Plan depends on it for: loop",
        ]
    )
    assert entry.form == "a"
    assert entry.depends_on == "loop"


def test_parse_form_a_without_depends_line() -> None:
    entry = _mod._parse_entry(["- Claim: it works", "  Evidence: docs quote"])
    assert entry.form == "a"
    assert entry.depends_on == ""


def test_parse_form_b_single_line() -> None:
    entry = _mod._parse_entry(
        [
            "- the CLI exposes --json — Evidence: `--help` — Depends on: parser "
            "— Route: VERIFIED — Observed-at: 2026-11-13T04:55:00Z — Kind: LIVE "
            "— Tool-call: Bash: tool --help — Observed-attempt: 2"
        ]
    )
    assert entry.form == "b"
    assert entry.claim == "the CLI exposes --json"
    assert entry.evidence == "`--help`"
    assert entry.depends_on == "parser"
    assert entry.route == "VERIFIED"
    assert entry.observed_at == "2026-11-13T04:55:00Z"
    assert entry.kind == "LIVE"
    assert entry.tool_call == "Bash: tool --help"
    assert entry.observed_attempt == "2"


def test_parse_form_b_em_dash_inside_claim_and_citation_is_harmless() -> None:
    entry = _mod._parse_entry(
        [
            "- a — b claim — Evidence: quote — with dash — Depends on: thing "
            "— Route: IMPACT"
        ]
    )
    assert entry.form == "b"
    assert entry.claim == "a — b claim"
    assert entry.evidence == "quote — with dash"
    assert entry.route == "IMPACT"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("Route", "IMPACT"),
        ("Observed-at", _BEFORE),
        ("Kind", "STATIC"),
        ("Tool-call", "Bash: other-command"),
    ],
)
def test_parse_form_b_duplicate_keys_are_structural_defects(
    key: str, value: str
) -> None:
    entry_text = f"{_new_entry()} — {key}: {value}"
    entry = _mod._parse_entry([entry_text])
    assert entry.structural_reasons == ("duplicate-key",)
    item = _one(_draft_eval(_draft(entry_text)))
    assert item["verdict"] == "STALE"
    assert item["reasons"] == ["duplicate-key"]


def test_parse_fully_stamped_empty_claim_is_unparseable() -> None:
    entry = _mod._parse_entry([_new_entry("")])
    assert entry.form == "unparseable"
    assert entry.claim != ""


def test_parse_form_b_joins_continuation_lines() -> None:
    entry = _mod._parse_entry(
        ["- claim — Evidence: line one", "  line two — Depends on: dep"]
    )
    assert entry.form == "b"
    assert entry.evidence == "line one line two"
    assert entry.depends_on == "dep"


def test_parse_form_c_prose() -> None:
    entry = _mod._parse_entry(["- the flag exists — seen in --help output"])
    assert entry.form == "c"
    assert entry.claim == "the flag exists"
    assert entry.evidence == "seen in --help output"
    assert entry.depends_on == "seen in --help output"


def test_parse_form_c_marker_before_evidence_is_not_form_b() -> None:
    entry = _mod._parse_entry(
        ["- the flag exists — Depends on: prose marker — Evidence: cited output"]
    )
    assert entry.form == "c"
    assert entry.claim == "the flag exists"


def test_parse_evidence_prefixed_form_c_without_depends_on() -> None:
    entry = _mod._parse_entry(
        ["- Evidence: prior observation — Evidence: cited output"]
    )
    assert entry.form == "c"
    assert entry.claim == "Evidence: prior observation"
    assert entry.evidence == "Evidence: cited output"


def test_parse_claim_label_never_reread_as_other_forms() -> None:
    entry = _mod._parse_entry(["- Claim: x — y"])
    assert entry.form == "unparseable"
    assert entry.claim != "x"


def test_parse_claim_label_with_empty_claim_is_unparseable() -> None:
    entry = _mod._parse_entry(["- Claim:", "  Evidence: y"])
    assert entry.form == "unparseable"


def test_parse_bare_text_is_unparseable() -> None:
    entry = _mod._parse_entry(["- some text with no separator"])
    assert entry.form == "unparseable"
    assert entry.claim == "some text with no separator"


def test_parse_leading_separator_is_unparseable() -> None:
    assert _mod._parse_entry(["-  — only evidence"]).form == "unparseable"


# ---------------------------------------------------------------------------
# draft: legacy fixtures (D) and Route inference
# ---------------------------------------------------------------------------


def test_fixture_impact_reason_is_carried() -> None:
    result = _draft_eval((_FIXTURES / "legacy_block_impact_reason.md").read_text())
    item = _one(result)
    assert item["claim"] == "[redacted] is unrelated to the ticket's diff"
    assert item["verdict"] == "CARRIED"
    assert item["reasons"] == []


def test_fixture_depends_on_it_is_stale_legacy() -> None:
    text = (_FIXTURES / "legacy_block_depends_on_it.md").read_text()
    entries = _mod._split_entries(text)
    parsed = _mod._parse_entry(entries[0])
    assert parsed.depends_on == "[redacted]"
    item = _one(_draft_eval(text))
    assert item["claim"] == "[redacted] behaves as the plan assumes"
    assert item["verdict"] == "STALE"
    assert item["reasons"] == ["legacy-no-metadata"]


def test_fixture_plain_evidence_is_stale_legacy() -> None:
    item = _one(_draft_eval((_FIXTURES / "legacy_block_plain_evidence.md").read_text()))
    assert item["claim"] == "[redacted] exists at the cited path"
    assert item["verdict"] == "STALE"
    assert item["reasons"] == ["legacy-no-metadata"]


@pytest.mark.parametrize(
    "evidence", ["Impact-Reason — changes nothing", "Impact-Reason: changes nothing"]
)
def test_legacy_impact_inference_form_a(evidence: str) -> None:
    item = _one(_draft_eval(_draft(_legacy_block("c", evidence))))
    assert item["verdict"] == "CARRIED"


@pytest.mark.parametrize(
    "evidence",
    ['R3 — "use the flag"', 'row:2 — "use the flag"'],
)
def test_legacy_resolution_inference_form_a(evidence: str) -> None:
    item = _one(_draft_eval(_draft(_legacy_block("c", evidence))))
    assert item["verdict"] == "CARRIED"


def test_legacy_resolution_without_quote_is_verified_stale() -> None:
    item = _one(_draft_eval(_draft(_legacy_block("c", "R3 — no quote here"))))
    assert item["verdict"] == "STALE"


def test_legacy_inference_form_b_without_metadata() -> None:
    text = _draft(
        "- b-impact — Evidence: Impact-Reason: orthogonal — Depends on: x",
        '- b-res — Evidence: R1 — "quoted" — Depends on: x',
        "- b-plain — Evidence: docs — Depends on: x",
    )
    by_claim = {i["claim"]: i for i in _draft_eval(text)}
    assert by_claim["b-impact"]["verdict"] == "CARRIED"
    assert by_claim["b-res"]["verdict"] == "CARRIED"
    assert by_claim["b-plain"]["verdict"] == "STALE"
    assert by_claim["b-plain"]["reasons"] == ["legacy-no-metadata"]


def test_legacy_inference_form_c() -> None:
    text = _draft(
        "- c-impact — Impact-Reason — orthogonal",
        '- c-res — row:4 — "quoted"',
        "- c-plain — saw it in the docs",
    )
    by_claim = {i["claim"]: i for i in _draft_eval(text)}
    assert by_claim["c-impact"]["verdict"] == "CARRIED"
    assert by_claim["c-res"]["verdict"] == "CARRIED"
    assert by_claim["c-plain"]["verdict"] == "STALE"
    assert by_claim["c-plain"]["reasons"] == ["legacy-no-metadata"]


def test_partly_stamped_form_b_is_new_form_not_legacy() -> None:
    text = _draft("- half — Evidence: docs — Depends on: x — Observed-at: " + _IN)
    item = _one(_draft_eval(text))
    assert item["verdict"] == "STALE"
    assert "legacy-no-metadata" not in _reasons(item)
    assert item["reasons"] == [
        "route-missing-or-invalid",
        "kind-missing-or-invalid",
        "tool-call-missing",
    ]


def test_route_only_form_b_is_new_form() -> None:
    text = _draft("- bare — Evidence: docs — Depends on: x — Route: MYSTERY")
    item = _one(_draft_eval(text))
    assert item["verdict"] == "STALE"
    assert item["reasons"] == [
        "route-missing-or-invalid",
        "observed-at-missing",
        "kind-missing-or-invalid",
        "tool-call-missing",
    ]


# ---------------------------------------------------------------------------
# draft: verdicts for new-form entries
# ---------------------------------------------------------------------------


def test_draft_fresh_when_observed_in_window() -> None:
    item = _one(_draft_eval(_draft(_new_entry())))
    assert item == {
        "claim": "the CLI exposes --json",
        "verdict": "FRESH",
        "reasons": [],
    }


def test_draft_fresh_with_observed_attempt() -> None:
    assert _one(_draft_eval(_draft(_new_entry(attempt="2"))))["verdict"] == "FRESH"


@pytest.mark.parametrize("route", [None, "MYSTERY", "VERIFIED extra"])
def test_draft_missing_or_invalid_route_is_stale(route: str | None) -> None:
    item = _one(_draft_eval(_draft(_new_entry(route=route))))
    assert item["verdict"] == "STALE"
    assert item["reasons"] == ["route-missing-or-invalid"]


def test_draft_static_pre_window_is_carried() -> None:
    item = _one(_draft_eval(_draft(_new_entry(kind="STATIC", observed_at=_BEFORE))))
    assert item["verdict"] == "CARRIED"
    assert item["reasons"] == []


def test_draft_repository_source_static_pre_window_is_carried() -> None:
    entry = _new_entry(kind="STATIC", observed_at=_BEFORE).replace(
        "`tool --help` shows --json", "src/api.py:12", 1
    )
    item = _one(_draft_eval(_draft(entry)))
    assert item["verdict"] == "CARRIED"
    assert item["reasons"] == []


def test_draft_live_pre_window_is_stale() -> None:
    item = _one(_draft_eval(_draft(_new_entry(kind="LIVE", observed_at=_BEFORE))))
    assert item["verdict"] == "STALE"
    assert item["reasons"] == ["observed-before-window"]


def test_draft_missing_kind_pre_window_is_stale() -> None:
    item = _one(_draft_eval(_draft(_new_entry(kind=None, observed_at=_BEFORE))))
    assert item["verdict"] == "STALE"
    assert item["reasons"] == ["observed-before-window", "kind-missing-or-invalid"]


def test_draft_static_in_future_is_stale() -> None:
    item = _one(_draft_eval(_draft(_new_entry(kind="STATIC", observed_at=_FUTURE))))
    assert item["verdict"] == "STALE"
    assert item["reasons"] == ["observed-in-future"]


@pytest.mark.parametrize("route", ["IMPACT", "RESOLUTION"])
def test_draft_impact_and_resolution_routes_are_carried(route: str) -> None:
    entry = (
        f"- routed claim{_EM}Evidence: orthogonal{_EM}Depends on: x{_EM}Route: {route}"
    )
    item = _one(_draft_eval(_draft(entry)))
    assert item["verdict"] == "CARRIED"
    assert item["reasons"] == []


def test_draft_route_carried_ignores_freshness_fields() -> None:
    entry = _new_entry(route="IMPACT", observed_at="garbage", kind=None, tool_call=None)
    assert _one(_draft_eval(_draft(entry)))["verdict"] == "CARRIED"


def test_draft_unknown_route_token_is_invalid() -> None:
    item = _one(_draft_eval(_draft(_new_entry(route="MYSTERY", observed_at=None))))
    assert item["verdict"] == "STALE"
    assert item["reasons"] == [
        "route-missing-or-invalid",
        "observed-at-missing",
    ]


def test_draft_reason_observed_at_missing_and_malformed() -> None:
    missing = _one(_draft_eval(_draft(_new_entry(observed_at=None))))
    assert missing["reasons"] == ["observed-at-missing"]
    malformed = _one(_draft_eval(_draft(_new_entry(observed_at="2026-11-13T04:55:00"))))
    assert malformed["reasons"] == ["observed-at-malformed"]


def test_draft_reason_tool_call_and_attempt() -> None:
    bad_call = _one(_draft_eval(_draft(_new_entry(tool_call="no colon"))))
    assert bad_call["reasons"] == ["tool-call-malformed"]
    bad_attempt = _one(_draft_eval(_draft(_new_entry(attempt="two"))))
    assert bad_attempt["reasons"] == ["observed-attempt-invalid"]


def test_draft_context_unavailable_is_stale_for_evaluated_entries() -> None:
    text = _draft(
        _new_entry("fresh-shaped"),
        _new_entry("impact", route="IMPACT"),
        _legacy_block("legacy plain", "docs"),
        "- bare text no separator",
    )
    by_claim = {i["claim"]: i for i in _draft_eval(text, None)}
    assert by_claim["fresh-shaped"]["verdict"] == "STALE"
    assert by_claim["fresh-shaped"]["reasons"] == ["context-unavailable"]
    assert by_claim["impact"]["verdict"] == "CARRIED"
    assert by_claim["impact"]["reasons"] == []
    assert by_claim["legacy plain"]["reasons"] == [
        "legacy-no-metadata",
        "context-unavailable",
    ]
    assert by_claim["bare text no separator"]["reasons"] == ["unparseable-legacy"]


# ---------------------------------------------------------------------------
# draft: relabeled-without-fresh-check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "re-verified this round",
        "I re-ran it in this dispatch",
        "independently confirmed this session",
        "reran against the live system this round",
    ],
)
def test_relabeled_stale_entry_gets_reason(phrase: str) -> None:
    entry = (
        f"- relabeled claim{_EM}Evidence: {phrase}. other sentence{_EM}Depends on: x"
    )
    item = _one(_draft_eval(_draft(entry)))
    assert item["verdict"] == "STALE"
    assert item["reasons"] == ["legacy-no-metadata", "relabeled-without-fresh-check"]


def test_relabeled_static_carried_entry_becomes_stale() -> None:
    entry = _new_entry(kind="STATIC", observed_at=_BEFORE).replace(
        "Evidence: `tool --help` shows --json",
        "Evidence: independently re-verified this round",
        1,
    )
    item = _one(_draft_eval(_draft(entry)))
    assert item["verdict"] == "STALE"
    assert item["reasons"] == ["relabeled-without-fresh-check"]


def test_relabeled_phrase_on_fresh_entry_is_not_flagged() -> None:
    entry = _new_entry().replace(
        "`tool --help` shows --json", "re-ran this session and saw --json", 1
    )
    assert _one(_draft_eval(_draft(entry)))["verdict"] == "FRESH"


def test_relabeled_needs_same_sentence() -> None:
    entry = (
        f"- split claim{_EM}Evidence: it was re-verified. The session ended"
        f"{_EM}Depends on: x"
    )
    item = _one(_draft_eval(_draft(entry)))
    assert item["reasons"] == ["legacy-no-metadata"]


def test_relabeled_never_touches_carried_routes() -> None:
    entry = (
        f"- impact claim{_EM}Evidence: independently of this dispatch{_EM}Depends on: x"
        f"{_EM}Route: IMPACT"
    )
    assert _one(_draft_eval(_draft(entry)))["verdict"] == "CARRIED"


# ---------------------------------------------------------------------------
# draft: unparseable-legacy
# ---------------------------------------------------------------------------


def test_unparseable_legacy_is_carried_and_never_stale() -> None:
    text = _draft(
        _new_entry("fresh one"),
        "- some text with no separator",
        _new_entry("impact one", route="IMPACT"),
    )
    result = _draft_eval(text)
    by_claim = {i["claim"]: i for i in result}
    odd = by_claim["some text with no separator"]
    assert odd["verdict"] == "CARRIED"
    assert odd["reasons"] == ["unparseable-legacy"]
    assert all(i["verdict"] != "STALE" for i in result)


def test_unparseable_claim_label_is_first_line_trimmed() -> None:
    item = _one(_draft_eval(_draft("-   spaced out text   ")))
    assert item["claim"] == "spaced out text"


def test_draft_emitted_reason_codes_are_a_closed_set() -> None:
    text = _draft(
        _new_entry("a", observed_at=None),
        _legacy_block("b", "docs"),
        "- unparseable",
        "- c — independently re-verified this round",
    )
    for item in _draft_eval(text):
        assert set(_reasons(item)) <= _REASON_CODES


# ---------------------------------------------------------------------------
# CLI contract (in-process main) and exit codes
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path, started: str = _STAGE_STARTED) -> Path:
    return _write_context(tmp_path, json.dumps({"stage_started_at": started}))


def _run_main(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> tuple[int, dict[str, object] | None, str]:
    code = _mod.main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else None
    return code, payload, captured.err


def test_main_scan_block_bound_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    block = tmp_path / "block.txt"
    block.write_text(_block(_pm_item()))
    code, payload, _ = _run_main(
        capsys, ["scan-block", "--file", str(block), "--context", str(_ctx(tmp_path))]
    )
    assert code == 0
    assert payload == {
        "items": [
            {"claim": "the API paginates results", "verdict": "BOUND", "reasons": []}
        ]
    }


def test_main_scan_block_unbound_exit_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    block = tmp_path / "block.txt"
    block.write_text(_block(_pm_item(overrides={"Kind": None})))
    code, payload, _ = _run_main(
        capsys, ["scan-block", "--file", str(block), "--context", str(_ctx(tmp_path))]
    )
    assert code == 1
    assert payload is not None
    assert set(payload) == {"items"}


def test_main_scan_block_missing_context_is_a_verdict_not_exit_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    block = tmp_path / "block.txt"
    block.write_text(_block(_pm_item()))
    code, payload, _ = _run_main(
        capsys,
        [
            "scan-block",
            "--file",
            str(block),
            "--context",
            str(tmp_path / "absent.json"),
        ],
    )
    assert code == 1
    assert _reasons(_items(payload)[0]) == ["context-unavailable"]


def test_main_scan_block_empty_block_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    block = tmp_path / "block.txt"
    block.write_text("nothing here\n")
    code, payload, _ = _run_main(
        capsys, ["scan-block", "--file", str(block), "--context", str(_ctx(tmp_path))]
    )
    assert code == 0
    assert payload == {"items": []}


def test_main_scan_block_unreadable_file_exit_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload, err = _run_main(
        capsys,
        [
            "scan-block",
            "--file",
            str(tmp_path / "absent.txt"),
            "--context",
            str(_ctx(tmp_path)),
        ],
    )
    assert code == 2
    assert payload is None
    assert "cannot read" in err


def test_main_draft_unreadable_plan_exit_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload, _ = _run_main(
        capsys,
        [
            "draft",
            "--plan",
            str(tmp_path / "absent.md"),
            "--context",
            str(_ctx(tmp_path)),
        ],
    )
    assert code == 2
    assert payload is None


def test_main_draft_undecodable_plan_exit_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = tmp_path / "plan.md"
    plan.write_bytes(b"\xff\xfe\x00bad")
    code, _, _ = _run_main(
        capsys, ["draft", "--plan", str(plan), "--context", str(_ctx(tmp_path))]
    )
    assert code == 2


def test_main_draft_fresh_and_carried_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(
        _draft(_new_entry("fresh"), _new_entry("imp", route="IMPACT"), "- unparseable")
    )
    code, payload, _ = _run_main(
        capsys, ["draft", "--plan", str(plan), "--context", str(_ctx(tmp_path))]
    )
    assert code == 0
    verdicts = [i["verdict"] for i in _items(payload)]
    assert verdicts == ["FRESH", "CARRIED", "CARRIED"]


def test_main_draft_stale_exit_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(_draft(_legacy_block("old", "docs")))
    code, payload, _ = _run_main(
        capsys, ["draft", "--plan", str(plan), "--context", str(_ctx(tmp_path))]
    )
    assert code == 1
    assert _items(payload)[0]["verdict"] == "STALE"


def test_main_draft_no_section_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n")
    code, payload, _ = _run_main(
        capsys, ["draft", "--plan", str(plan), "--context", str(_ctx(tmp_path))]
    )
    assert code == 0
    assert payload == {"items": []}


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["scan-block"],
        ["scan-block", "--file", "x"],
        ["draft", "--plan", "x"],
        ["bogus", "--file", "x", "--context", "y"],
        ["scan-block", "--file", "x", "--context", "y", "--projects-dir", "z"],
    ],
)
def test_main_usage_errors_exit_two(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _mod.main(argv)
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# Subprocess CLI contract (real clock)
# ---------------------------------------------------------------------------


def _real_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _cli_context(tmp_path: Path) -> Path:
    started = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    return _write_context(tmp_path, json.dumps({"stage_started_at": started}))


def _stamp(delta: timedelta) -> str:
    return (_real_now() + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_cli(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return run_guard_script_cli(_SCRIPT, argv)


def test_cli_scan_block_bound(tmp_path: Path) -> None:
    block = tmp_path / "block.txt"
    block.write_text(
        _block(_pm_item(overrides={"Observed-at": _stamp(-timedelta(minutes=1))}))
    )
    proc = _run_cli(
        ["scan-block", "--file", str(block), "--context", str(_cli_context(tmp_path))]
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert set(payload) == {"items"}
    assert payload["items"][0]["verdict"] == "BOUND"
    assert set(payload["items"][0]) == {"claim", "verdict", "reasons"}


def test_cli_scan_block_unbound_before_window(tmp_path: Path) -> None:
    block = tmp_path / "block.txt"
    block.write_text(
        _block(_pm_item(overrides={"Observed-at": _stamp(-timedelta(hours=3))}))
    )
    proc = _run_cli(
        ["scan-block", "--file", str(block), "--context", str(_cli_context(tmp_path))]
    )
    assert proc.returncode == 1
    item = json.loads(proc.stdout)["items"][0]
    assert item["verdict"] == "UNBOUND"
    assert item["reasons"] == ["observed-before-window"]


def test_cli_draft_fresh_and_stale(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(_draft(_new_entry(observed_at=_stamp(-timedelta(minutes=1)))))
    ctx = _cli_context(tmp_path)
    fresh = _run_cli(["draft", "--plan", str(plan), "--context", str(ctx)])
    assert fresh.returncode == 0, fresh.stderr
    assert json.loads(fresh.stdout)["items"][0]["verdict"] == "FRESH"
    plan.write_text(_draft(_new_entry(observed_at=_stamp(-timedelta(hours=3)))))
    stale = _run_cli(["draft", "--plan", str(plan), "--context", str(ctx)])
    assert stale.returncode == 1
    assert json.loads(stale.stdout)["items"][0]["verdict"] == "STALE"


def test_cli_missing_input_and_bad_usage_exit_two(tmp_path: Path) -> None:
    ctx = _cli_context(tmp_path)
    missing = _run_cli(
        ["draft", "--plan", str(tmp_path / "absent.md"), "--context", str(ctx)]
    )
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert _run_cli([]).returncode == 2
    rejected = _run_cli(
        [
            "scan-block",
            "--file",
            str(tmp_path),
            "--context",
            str(ctx),
            "--projects-dir",
            str(tmp_path),
        ]
    )
    assert rejected.returncode == 2
