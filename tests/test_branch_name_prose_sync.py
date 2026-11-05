"""Guard test (#2948): every stage command that uses ``<branch-name>`` defines it.

Workers learn the ticket's branch from prose, not from Python. Before #2948
``<branch-name>`` was a bare placeholder across the auto-dev stage docs with no
definition, so a worker on an adopted (non-prefix) branch would guess
``dev/<ticket>``, find no PR, and open a second one. Each doc that uses the
placeholder now carries one byte-identical "Branch resolution" paragraph that
reads the row's branch from ``queue_metadata.branch`` in
``.claude/cw-context.json`` and falls back to ``<branch-prefix>/<ticket-id>``.

Style follows ``tests/test_changelog_gate_prose_sync.py`` (the literal must be
in every copy), but the doc set is globbed rather than listed, so a new stage
doc that starts using ``<branch-name>`` is caught without editing this file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.conftest import _COMMANDS_ROOT

if TYPE_CHECKING:
    from pathlib import Path

_PLACEHOLDER = "<branch-name>"
_LEAD = "**Branch resolution.** "
_PARAGRAPH = (
    "**Branch resolution.** `<branch-name>` is this ticket's own branch: run "
    "`jq -r '.queue_metadata.branch // empty' .claude/cw-context.json` from the "
    "cw session worktree root and use what it prints. When it prints nothing "
    "(the file or key is absent, or the value is null), use "
    "`<branch-prefix>/<ticket-id>`, where `<branch-prefix>` is the effective "
    "prefix (`--branch-prefix` if given, else the client's "
    "`feature_branch_prefix`, default `dev`). Do not take it from "
    "`git branch --show-current`: an isolation worktree may sit on an "
    "`agent-<hash>` branch."
)
_SHOW_CURRENT = "git branch --show-current"
_SHOW_CURRENT_GUARD = "Do not take it from `"
_FORBIDDEN_WORDS = ("spawn", "dispatch")
_EXPECTED_DOCS = frozenset(
    {
        "auto-dev.md",
        "auto-dev-impl.md",
        "auto-dev-review.md",
        "auto-dev-finalize.md",
        "auto-dev-intake.md",
        "auto-dev-impl-appendix.md",
        "auto-dev-review-appendix.md",
        "auto-dev-finalize-appendix.md",
        "auto-dev-intake-appendix.md",
    }
)


def _stage_docs() -> dict[str, str]:
    paths: list[Path] = sorted(_COMMANDS_ROOT.glob("auto-dev*.md"))
    return {path.name: path.read_text(encoding="utf-8") for path in paths}


def _without_paragraph(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if line != _PARAGRAPH)


def _uses_placeholder(text: str) -> bool:
    """True when *text* uses ``<branch-name>`` outside the paragraph itself."""
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


def test_every_doc_using_the_placeholder_carries_the_exact_paragraph() -> None:
    for name, text in _stage_docs().items():
        if not _uses_placeholder(text):
            continue
        assert _paragraph_lines(text) == [_PARAGRAPH], (
            f"{name} uses {_PLACEHOLDER} but is missing (or has drifted from) "
            f"the Branch resolution paragraph {_PARAGRAPH!r}"
        )


def test_paragraph_doc_set_is_pinned() -> None:
    """A floor: deleting every use and every paragraph cannot pass."""
    docs = _stage_docs()
    carrying = {name for name, text in docs.items() if _paragraph_lines(text)}
    using = {name for name, text in docs.items() if _uses_placeholder(text)}
    assert using >= _EXPECTED_DOCS
    assert carrying == using


def test_paragraph_sits_in_the_preamble_before_any_other_use() -> None:
    """Outside every heading-sliced region other doc tests index into."""
    for name, text in _stage_docs().items():
        if _paragraph_lines(text):
            assert _placement_ok(text), name


def test_paragraph_never_sources_the_branch_from_show_current() -> None:
    start = 0
    while (idx := _PARAGRAPH.find(_SHOW_CURRENT, start)) >= 0:
        assert _PARAGRAPH[:idx].endswith(_SHOW_CURRENT_GUARD)
        start = idx + 1


def test_paragraph_avoids_spawn_call_site_vocabulary() -> None:
    """``tests/test_impl_read_only_helper_docs.py`` scans every command-doc
    paragraph for spawn call sites; ``agent-<hash>`` would match its agent
    clause if the paragraph also named a spawn or dispatch."""
    lowered = _PARAGRAPH.lower()
    assert not any(word in lowered for word in _FORBIDDEN_WORDS)


def test_helpers_detect_missing_drifted_and_misplaced_paragraphs() -> None:
    body = f"use {_PLACEHOLDER} here\n"
    missing = f"# Title\n\n{body}"
    drifted = f"# Title\n\n{_LEAD}`<branch-name>` is whatever.\n\n{body}"
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
