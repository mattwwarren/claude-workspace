"""Doc-structure guards for the mypy-baseline type-check gate.

The Stage 2 type-check gate honors a repo-maintained mypy baseline, but only
if every downstream re-check applies the same rule. Stage 2's agent prompt,
Step 2.5 gate 4, and Stage 3's fix-loop gate (3b) must all point at the one
appendix section that defines detection and comparison — otherwise a worker
that correctly leaves baselined debt unfixed is failed by a stricter re-check
downstream. The gate prose itself has no parser (sibling pattern:
`tests/test_scope_conformance_gate_docs.py`); the deferred-findings entry it
tells the orchestrator to hand-write does, so that shape is round-tripped
through the real parser here.
"""

from cw.review_adjudication._deferred_md import (
    _DEFERRED_MD_HEADING,
    _DEFERRED_MD_PROVENANCE,
    _DEFERRED_MD_TITLE,
    _DEFERRED_SENTINEL,
    parse_deferred_findings_md,
)
from tests.conftest import _cmd

_SECTION = "Type check gate: mypy baseline detection and comparison"


def _flat(name: str) -> str:
    """Command doc with whitespace collapsed, so prose line-wraps don't matter."""
    return " ".join(_cmd(name).split())


def test_appendix_defines_baseline_section() -> None:
    """The appendix owns detection, the no-growth check, and deferral routing."""
    content = _flat("auto-dev-impl-appendix.md")
    assert f"## {_SECTION}" in content
    assert "as `MYPY_BASELINE_CMD`" in content
    assert "as `MYPY_BASELINE_FILE`" in content
    assert "The baseline may never grow in this diff." in content
    assert "DEFERRED-REVIEW-FINDINGS" in content


def test_every_mypy_gate_references_the_baseline_section() -> None:
    """Stage 2 prompt, Step 2.5 gate 4, and Stage 3 gate 3b share one rule."""
    impl = _flat("auto-dev-impl.md")
    assert impl.count(_SECTION) >= 2, "Stage 2 prompt and gate 4 must both cite it"
    assert _SECTION in _flat("auto-dev-review.md")


def test_strict_override_names_global_instructions() -> None:
    """The operator's global instructions can switch the baseline branch off."""
    impl = _flat("auto-dev-impl.md")
    assert "Strict override check (run first)." in impl
    assert "~/.claude/CLAUDE.md" in impl


def test_appendix_deferred_skeleton_carries_parser_header() -> None:
    """A hand-created `.cw/deferred-findings.md` must carry the exact title and
    provenance lines `parse_deferred_findings_md` requires, or Stage 3's next
    `cw review adjudicate` hard-fails instead of merging."""
    content = _cmd("auto-dev-impl-appendix.md")
    assert _DEFERRED_MD_TITLE in content
    assert _DEFERRED_MD_PROVENANCE in content


def test_documented_skeleton_and_entry_parse_as_one_deferral() -> None:
    """The skeleton + entry shape the appendix documents round-trips through
    the fail-closed parser as exactly one `defer` adjudication."""
    text = (
        f"{_DEFERRED_MD_TITLE}\n{_DEFERRED_MD_PROVENANCE}\n\n"
        f"{_DEFERRED_MD_HEADING}\n\n"
        f"<!-- {_DEFERRED_SENTINEL}\n"
        "- severity: SHOULD_FIX\n"
        '  summary: "pre-existing baselined mypy errors in src/app.py"\n'
        "  file: src/app.py\n"
        '  rationale: "baselined debt in a file this change touched; not '
        'introduced by it: src/app.py:12 [arg-type]"\n'
        f"{_DEFERRED_SENTINEL} -->\n"
    )

    entries = parse_deferred_findings_md(text)

    assert len(entries) == 1
    assert entries[0].outcome == "defer"
    assert entries[0].file == "src/app.py"
