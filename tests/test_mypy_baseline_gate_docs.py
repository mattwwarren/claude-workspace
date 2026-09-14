"""Doc-structure guards for the mypy-baseline type-check gate.

The Stage 2 type-check gate honors a repo-maintained mypy baseline, but only
if every downstream re-check applies the same rule. Stage 2's agent prompt,
Step 2.5 gate 4, and Stage 3's fix-loop gate (3b) must all point at the one
appendix section that defines detection and comparison — otherwise a worker
that correctly leaves baselined debt unfixed is failed by a stricter re-check
downstream. Pure prose consumed by the orchestrating session; there is no
parser (sibling pattern: `tests/test_scope_conformance_gate_docs.py`).
"""

from tests.conftest import _cmd

_SECTION = "Type check gate: mypy baseline detection and comparison"


def test_appendix_defines_baseline_section() -> None:
    """The appendix owns detection, the no-growth check, and deferral routing."""
    content = _cmd("auto-dev-impl-appendix.md")
    assert f"## {_SECTION}" in content
    assert "MYPY_BASELINE_CMD" in content
    assert "The baseline may never grow in this diff." in content
    assert "DEFERRED-REVIEW-FINDINGS" in content


def test_every_mypy_gate_references_the_baseline_section() -> None:
    """Stage 2 prompt, Step 2.5 gate 4, and Stage 3 gate 3b share one rule."""
    impl = _cmd("auto-dev-impl.md")
    assert impl.count(_SECTION) >= 2, "Stage 2 prompt and gate 4 must both cite it"
    assert _SECTION in _cmd("auto-dev-review.md")


def test_strict_override_names_global_instructions() -> None:
    """The operator's global instructions can switch the baseline branch off."""
    impl = _cmd("auto-dev-impl.md")
    assert "Strict override check (run first)." in impl
    assert "~/.claude/CLAUDE.md" in impl
