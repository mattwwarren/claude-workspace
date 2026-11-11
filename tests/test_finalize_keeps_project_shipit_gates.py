"""Guard tests: `--skip-review` must not waive a project ship-it's own pre-PR gates.

Finalize delegates PR creation to `/prep-pr --skip-review`, and `/prep-pr`
hands off to the project's `/ship-it`. Headless finalize children read
`--skip-review` (plus the recorded cw review approval and the verify-tip fence)
as satisfying the project ship-it's own pre-PR reviewer gate, so pull requests
were opened without that gate ever running, and a risk label the ship-it copies
from the reviewer's output was filled in by the worker's own guess instead.
Other children honored the gate and blocked, so behavior split by run.

The fix states in both commands that `--skip-review` covers only `/prep-pr`'s
own self-review loop, that project ship-it gates run as written, and that a
value copied from a gate that did not run is never invented. Prose guards in
the established `.claude/commands/*.md` precedent: pure substring/region
assertions over the doc text, using the shared readers from
``tests/conftest.py``.
"""

from __future__ import annotations

from tests._shared.workflow_text import _cmd, _step4c2_section


def test_finalize_brief_keeps_project_shipit_gates() -> None:
    section = _step4c2_section()
    assert "Project ship-it gates are not waived (mandatory)" in section
    assert "`--skip-review` skips only `/prep-pr`'s own self-review loop" in section
    assert (
        "do NOT satisfy a project ship-it gate unless that ship-it itself says they do"
        in section
    )
    assert "does not create the PR" in section
    assert (
        '"Run every pre-PR gate the project ship-it defines exactly as it defines it; '
        '`--skip-review` waives none of them."'
    ) in section
    assert 'gate: "Step 8 ship-it: <gate name>"' in section


def test_finalize_brief_forbids_inventing_gate_output() -> None:
    section = _step4c2_section()
    assert "never fills in a value the ship-it copies from a gate's output" in section


def test_prep_pr_step8_skip_review_never_reaches_project_shipit() -> None:
    text = _cmd("prep-pr.md")
    assert "`--skip-review` never reaches the project ship-it." in text
    assert "in every mode including `--headless`" in text
    assert 'gate: "Step 8 ship-it: <gate name>"' in text
    assert (
        "never fill in a value the ship-it copies from a gate that did not run" in text
    )
