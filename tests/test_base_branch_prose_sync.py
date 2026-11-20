"""Guard test (#3781): every stage command that uses ``<base>`` defines it.

A ticket's merge target is not always the repository default branch: a row can
carry ``base_branch`` (an epic branch), and ``cw`` writes the resolved value into
``.claude/cw-context.json`` as the top-level ``base_branch`` key. Before #3781
the stage docs hard-coded ``origin/main`` and ``--base main`` at every fetch,
merge, diff range and ``gh pr create`` site, so a ticket on an epic branch was
probed, merged and opened against the wrong target. Each ``auto-dev*.md`` doc
that uses the ``<base>`` placeholder now carries one byte-identical "Base
resolution" paragraph that reads the context key and falls back to the
repository default branch (``origin/HEAD``).

``prep-pr.md`` and ``ship-it.md`` are given ``--base`` by their caller, so they
carry no paragraph; they are scanned only for a reintroduced literal.

Two guards live here. The paragraph guard mirrors
``tests/test_branch_name_prose_sync.py`` (the doc set is globbed, so a new stage
doc that starts using ``<base>`` is caught without editing this file). The
literal scan fails on a reintroduced hard-coded ``main`` at a merge-target site,
with a per-line allowlist whose every entry carries a reason and must still match
a line (a stale entry fails, so the allowlist cannot rot).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tests._shared.workflow_text import _COMMANDS_ROOT

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_PLACEHOLDER = "<base>"
_LEAD = "**Base resolution.** "
_PARAGRAPH = (
    "**Base resolution.** `<base>` is the branch this ticket merges into: run "
    "`jq -r '.base_branch // .world_state_snapshot.origin_main_branch // empty' "
    ".claude/cw-context.json` from the cw session worktree root and use what it "
    "prints. When it prints nothing (the file or both keys are absent), use the "
    "repository default branch: the name "
    "`git symbolic-ref --short refs/remotes/origin/HEAD` prints with its leading "
    "`origin/` removed. Every fetch, merge, diff range, `--base` argument and "
    "open-PR filter in this document that names the merge target means `<base>`; "
    "never type a branch name for it."
)
# The words `tests/test_impl_read_only_helper_docs.py` scans paragraphs for.
_FORBIDDEN_WORDS = ("spawn", "dispatch", "agent")
_EXPECTED_DOCS = frozenset(
    {
        "auto-dev.md",
        "auto-dev-impl.md",
        "auto-dev-review.md",
        "auto-dev-review-appendix.md",
        "auto-dev-finalize.md",
        "auto-dev-finalize-appendix.md",
    }
)
_NO_PARAGRAPH_DOCS = ("prep-pr.md", "ship-it.md")

# A hard-coded `main` at a merge-target site. P1 is the unambiguous token set;
# P2 is the prose shape ("fetch main", "sync with main") that carries no token.
_P1 = (
    r"origin/main|--base main|fetch origin main|rev-parse main|\.\.main"
    r'|"base": "main"|BASE:-main|push origin main|merge-probe --base main'
)
_P2 = (
    r"(?:^|[^A-Za-z0-9/_-])"
    r"(?:fetch|merge|merging|checks|behind|with|vs|from|to|of|into|on|against"
    r"|default to|sync-with)[ -]`?main\b"
)
_P1_RE = re.compile(_P1, re.IGNORECASE)
_P2_RE = re.compile(_P2, re.IGNORECASE)
_NOT_A_TARGET = re.compile(r"main (?:session|checkout|thread)", re.IGNORECASE)

_HEALTH_GATE = (
    "operator-checkout health gate (dispatch/gating/freshness.py): compares the "
    "operator's local main with origin/main, not a ticket merge target"
)
_PROTECTED_BRANCH = (
    "protected-branch check on the branch being prepped, not a merge target"
)
# (doc, substring of the hit line) -> reason. Every entry must match a hit line.
_ALLOWLIST: dict[tuple[str, str], str] = {
    ("auto-dev-intake.md", "Fails fast when the local `main` branch"): _HEALTH_GATE,
    ("auto-dev-intake.md", "fetch origin main --quiet"): _HEALTH_GATE,
    ("auto-dev-intake.md", "LOCAL_MAIN=$(git -C"): _HEALTH_GATE,
    ("auto-dev-intake.md", "ORIGIN_MAIN=$(git -C"): _HEALTH_GATE,
    ("auto-dev-intake.md", "rev-list --count origin/main..main"): _HEALTH_GATE,
    ("auto-dev-intake.md", "rev-list --count main..origin/main"): _HEALTH_GATE,
    ("auto-dev-intake-appendix.md", "| Sync now |"): _HEALTH_GATE,
    ("auto-dev-intake-appendix.md", "LOCAL_MAIN=$(git -C"): _HEALTH_GATE,
    ("auto-dev-intake-appendix.md", "Local main is not in sync"): _HEALTH_GATE,
    ("auto-dev-intake-appendix.md", '"recovery_hint": "Orchestrator'): _HEALTH_GATE,
    ("auto-dev-intake-appendix.md", "typically one `git push origin main`"): (
        _HEALTH_GATE
    ),
    ("auto-dev.md", "local_main_diverged_from_origin"): (
        "blocker-table row for the operator-checkout health gate"
    ),
    ("auto-dev-finalize.md", "never inherits one from `main`"): (
        "descriptive only: a fresh worktree does not inherit an untracked file "
        "from the main checkout"
    ),
    ("prep-pr.md", "1. **Not on main/master:**"): _PROTECTED_BRANCH,
    ("prep-pr.md", "If on `main` or `master`, abort with"): _PROTECTED_BRANCH,
}


def _stage_docs() -> dict[str, str]:
    paths: list[Path] = sorted(_COMMANDS_ROOT.glob("auto-dev*.md"))
    return {path.name: path.read_text(encoding="utf-8") for path in paths}


def _scanned_docs() -> dict[str, str]:
    docs = _stage_docs()
    for name in _NO_PARAGRAPH_DOCS:
        docs[name] = (_COMMANDS_ROOT / name).read_text(encoding="utf-8")
    return docs


def _without_paragraph(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if line != _PARAGRAPH)


def _uses_placeholder(text: str) -> bool:
    """True when *text* uses ``<base>`` outside the paragraph itself."""
    return _PLACEHOLDER in _without_paragraph(text)


def _paragraph_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(_LEAD)]


def _placement_ok(text: str) -> bool:
    """The paragraph precedes the first ``## `` heading and every other use."""
    start = text.find(f"\n{_PARAGRAPH}\n")
    if start < 0:
        return False
    heading = text.find("\n## ")
    return _PLACEHOLDER not in text[:start] and (heading < 0 or start < heading)


def _literal_hits(text: str) -> list[str]:
    """Lines that hard-code ``main`` where a doc should say ``<base>``."""
    return [
        line
        for line in text.splitlines()
        if _P1_RE.search(line)
        or (_P2_RE.search(line) and not _NOT_A_TARGET.search(line))
    ]


def _unallowed_hits(
    docs: Mapping[str, str], allowlist: Mapping[tuple[str, str], str]
) -> list[str]:
    return [
        f"{name}: {line.strip()[:160]}"
        for name, text in docs.items()
        for line in _literal_hits(text)
        if not any(
            doc == name and needle in line
            for (doc, needle), _reason in allowlist.items()
        )
    ]


def _stale_entries(
    docs: Mapping[str, str], allowlist: Mapping[tuple[str, str], str]
) -> list[tuple[str, str]]:
    return [
        (doc, needle)
        for doc, needle in allowlist
        if not any(needle in line for line in _literal_hits(docs.get(doc, "")))
    ]


def test_every_doc_using_the_placeholder_carries_the_exact_paragraph() -> None:
    for name, text in _stage_docs().items():
        if not _uses_placeholder(text):
            continue
        assert _paragraph_lines(text) == [_PARAGRAPH], (
            f"{name} uses {_PLACEHOLDER} but is missing (or has drifted from) "
            f"the Base resolution paragraph {_PARAGRAPH!r}"
        )


def test_paragraph_doc_set_is_pinned() -> None:
    """A floor: deleting every use and every paragraph cannot pass."""
    docs = _stage_docs()
    carrying = {name for name, text in docs.items() if _paragraph_lines(text)}
    using = {name for name, text in docs.items() if _uses_placeholder(text)}
    assert using == _EXPECTED_DOCS
    assert carrying == using


def test_paragraph_sits_in_the_preamble_before_any_other_use() -> None:
    """Outside every heading-sliced region other doc tests index into."""
    for name, text in _stage_docs().items():
        if _paragraph_lines(text):
            assert _placement_ok(text), name


def test_prep_pr_and_ship_it_carry_no_paragraph() -> None:
    """Their caller passes ``--base``; a copied paragraph would be a second source."""
    docs = _scanned_docs()
    for name in _NO_PARAGRAPH_DOCS:
        assert _paragraph_lines(docs[name]) == [], name


def test_paragraph_avoids_spawn_call_site_vocabulary() -> None:
    """``tests/test_impl_read_only_helper_docs.py`` scans every command-doc
    paragraph for spawn call sites."""
    lowered = _PARAGRAPH.lower()
    assert not any(word in lowered for word in _FORBIDDEN_WORDS)


def test_paragraph_reads_the_context_key_then_origin_head() -> None:
    jq = ".base_branch // .world_state_snapshot.origin_main_branch // empty"
    fallback = "git symbolic-ref --short refs/remotes/origin/HEAD"
    assert _PARAGRAPH.index(jq) < _PARAGRAPH.index(fallback)


def test_no_doc_reintroduces_a_hard_coded_main_at_a_merge_target() -> None:
    unallowed = _unallowed_hits(_scanned_docs(), _ALLOWLIST)
    assert unallowed == [], (
        "a hard-coded `main` where the doc should say `<base>` (or add an "
        "allowlist entry with a reason if it is not a merge target):\n"
        + "\n".join(unallowed)
    )


def test_every_allowlist_entry_still_matches_a_line() -> None:
    assert _stale_entries(_scanned_docs(), _ALLOWLIST) == []


def test_every_allowlist_entry_carries_a_reason() -> None:
    assert all(reason.strip() for reason in _ALLOWLIST.values())


def test_prep_pr_reads_claude_md_from_origin_head_not_a_branch_name() -> None:
    """The default-branch ``CLAUDE.md`` read follows ``origin/HEAD`` already."""
    text = (_COMMANDS_ROOT / "prep-pr.md").read_text(encoding="utf-8")
    bullets = [
        line
        for line in text.splitlines()
        if line.startswith("- **Source: the default branch's committed `CLAUDE.md`.**")
    ]
    assert len(bullets) == 1
    assert "(whatever `origin/HEAD` points at)" in bullets[0]
    assert _literal_hits(bullets[0]) == []


def test_helpers_detect_missing_drifted_and_misplaced_paragraphs() -> None:
    body = f"use {_PLACEHOLDER} here\n"
    missing = f"# Title\n\n{body}"
    drifted = f"# Title\n\n{_LEAD}`<base>` is whatever.\n\n{body}"
    placed = f"# Title\n\n{_PARAGRAPH}\n\n{body}## Step\n"
    late = f"# Title\n\n{body}\n{_PARAGRAPH}\n\n## Step\n"
    under_heading = f"# Title\n\n## Step\n\n{_PARAGRAPH}\n\n{body}"

    assert _uses_placeholder(missing)
    assert _paragraph_lines(missing) == []
    assert _paragraph_lines(drifted) != [_PARAGRAPH]
    assert _paragraph_lines(placed) == [_PARAGRAPH]
    assert _placement_ok(placed)
    assert not _placement_ok(late)
    assert not _placement_ok(under_heading)
    assert not _uses_placeholder(f"# Title\n\n{_PARAGRAPH}\n")


def test_literal_scan_flags_a_reintroduced_origin_main() -> None:
    doc = "# Title\n\ngit fetch origin <base>\ngit merge origin/main --no-edit\n"
    assert _literal_hits(doc) == ["git merge origin/main --no-edit"]
    assert _unallowed_hits({"synthetic.md": doc}, {}) == [
        "synthetic.md: git merge origin/main --no-edit"
    ]


def test_literal_scan_flags_the_tokenless_prose_shapes() -> None:
    doc = "sync with main before the PR\nthe operator's main checkout\n"
    assert _literal_hits(doc) == ["sync with main before the PR"]


def test_literal_scan_flags_a_literal_base_argument_and_json_value() -> None:
    doc = '/prep-pr --base main\n"base": "main"\n'
    assert len(_literal_hits(doc)) == 2


def test_literal_scan_leaves_the_placeholder_alone() -> None:
    doc = "git merge origin/<base> --no-edit\n/prep-pr --base <base>\n"
    assert _literal_hits(doc) == []


def test_an_allowlist_entry_suppresses_only_its_own_doc_and_line() -> None:
    doc = "git push origin main\n"
    allow = {("a.md", "git push origin main"): "reason"}
    assert _unallowed_hits({"a.md": doc}, allow) == []
    assert _unallowed_hits({"b.md": doc}, allow) != []


def test_a_stale_allowlist_entry_is_reported() -> None:
    docs = {"a.md": "git push origin main\n", "b.md": "nothing here\n"}
    allow = {
        ("a.md", "git push origin main"): "live",
        ("a.md", "git fetch origin main"): "stale: no such line",
        ("b.md", "git push origin main"): "stale: doc has no hit",
        ("missing.md", "origin/main"): "stale: no such doc",
    }
    assert _stale_entries(docs, allow) == [
        ("a.md", "git fetch origin main"),
        ("b.md", "git push origin main"),
        ("missing.md", "origin/main"),
    ]
