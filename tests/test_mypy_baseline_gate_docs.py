"""Doc-structure guards for the mypy-baseline type-check gate.

The Stage 2 type-check gate honors a repo-maintained mypy baseline, but only
if every downstream re-check applies the same rule. Stage 2's agent prompt,
Step 2.5 gate 4, and Stage 3's fix-loop gate (3b) must all point at the one
appendix section that defines detection and comparison — otherwise a worker
that correctly leaves baselined debt unfixed is failed by a stricter re-check
downstream. The gate prose itself has no parser (sibling pattern:
`tests/test_scope_conformance_gate_docs.py`); the deferred-findings entry it
tells the orchestrator to hand-write does, so that shape is round-tripped
through the real parser here. Gate 4's failure text is pinned to the Stage 2
fix-all rule the same way: both slices must quote one sentence, so the two
cannot drift apart.
"""

from cw.review_adjudication._deferred_md import (
    _DEFERRED_MD_HEADING,
    _DEFERRED_MD_PROVENANCE,
    _DEFERRED_MD_TITLE,
    _DEFERRED_SENTINEL,
    parse_deferred_findings_md,
)
from tests._shared.workflow_text import _cmd, _norm, _section_between

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


_FIX_ALL_RULE = (
    "fix every mypy error in touched files, pre-existing ones included — "
    "treat those as active bugs, not noise to inherit"
)
_STAGE2_START = "Type check gate (do NOT declare complete until clean)"
_STAGE2_END = "**Override sweep.**"
_GATE4_START = "4. **Mypy/ruff clean on touched files**"
_GATE4_END = "5. **Incremental commit discipline**"


def _stage2_gate() -> str:
    return _norm(_section_between(_cmd("auto-dev-impl.md"), _STAGE2_START, _STAGE2_END))


def _gate4() -> str:
    return _norm(_section_between(_cmd("auto-dev-impl.md"), _GATE4_START, _GATE4_END))


def test_fix_all_rule_is_quoted_in_stage2_gate_and_gate4() -> None:
    """Gate 4's failure text quotes the Stage 2 fix-all rule as written."""
    assert _FIX_ALL_RULE in _stage2_gate()
    assert _FIX_ALL_RULE in _gate4()


def test_gate4_failure_text_names_scope_and_verbatim_errors() -> None:
    """Gate 4's failure text carries the verbatim errors and the touched-file scope."""
    gate4 = _gate4()
    assert "Gate 4 failure text" in gate4
    assert "verbatim mypy output" in gate4
    assert "names the scope: the touched files the re-run covered" in gate4


def test_gate4_forbids_fork_point_delta_framing() -> None:
    """No-baseline and strict-override branches never reason from fork-point deltas."""
    gate4 = _gate4()
    assert "Never reason from fork-point error-count deltas" in gate4
    assert "never describe pre-existing errors in a touched file as tolerated" in gate4
    assert (
        "`$FORK_POINT` is used only for baseline detection "
        "and the baseline no-growth check" in gate4
    )


def test_review_gate_3b_still_delegates_to_gate4() -> None:
    """Stage 3 gate 3b inherits gate 4's rule by reference, not by copy."""
    assert "Step 2.5 gate 4 applies" in _flat("auto-dev-review.md")
