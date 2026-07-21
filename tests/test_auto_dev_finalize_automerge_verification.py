"""Guard tests: auto-merge arm-then-verify at both Step 4c and Step 4d (#1140).

Pure-markdown assertions over the auto-dev finalize pipeline instruction file
and the headless contract doc. This repo has an established convention (see
``tests/test_auto_dev_model_pins.py``, ``tests/test_auto_dev_preflight_resolutions.py``,
and ``tests/test_unavailability.py``) of a small **private per-file**
``_cmd()``-style helper that reads ``.claude/commands/*.md`` prose and asserts
substrings/regions, rather than a shared ``conftest.py`` fixture. This file is
the 4th consumer of that same pattern.

Root cause pinned here: ``gh pr merge --auto`` can report success while the
read-back (``autoMergeRequest``) stays null — the prior prose either had no
headless branch (Step 4c) or no verification at all (Step 4d reuse path).
Both sites must now emit a ``blocked`` sentinel with ``blocker.reason:
"automerge_not_armed"`` on a failed verify, using the ``pr_info`` (not
``pr``) convention so the parser's ``_coerce_blocked_with_pr`` doesn't
silently rewrite ``status`` to ``merge_pending`` (see
``docs/headless-contract.md`` §6 "Parse-boundary coercions").
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parent.parent
COMMANDS = ROOT / ".claude" / "commands"
DOCS = ROOT / "docs"


def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text()


def _doc(name: str) -> str:
    return (DOCS / name).read_text()


def _step4c_section() -> str:
    content = _cmd("auto-dev-finalize.md")
    start = content.index("Main-session re-verification (do not skip):")
    end = content.index("### Step 4c.5")
    return content[start:end]


def _step4d_enable_automerge_section() -> str:
    content = _cmd("auto-dev-finalize.md")
    start = content.index("3. **Enable auto-merge:**")
    end = content.index("4. **Post to Linear:**")
    return content[start:end]


def test_step4c_reverification_has_headless_branch() -> None:
    """The bare AskUserQuestion at Step 4c's re-verification gap must now

    carry an explicit ``**Headless:**`` sub-bullet — every other
    AskUserQuestion in this file already does (#1140 root cause).
    """
    assert "**Headless:**" in _step4c_section()


def test_step4c_automerge_not_armed_sentinel_present() -> None:
    section = _step4c_section()
    assert '"reason": "automerge_not_armed"' in section
    assert '"stage_reached": "stage5_post_create"' in section
    assert '"pr_info"' in section


def test_step4c_automerge_not_armed_uses_pr_info_not_pr_object() -> None:
    """Regression guard for the pr/pr_info coercion trap (#1140).

    Within the new sentinel template block, ``pr`` must be explicitly null
    alongside a populated ``pr_info`` — never a populated ``pr`` object,
    which the parser's ``_coerce_blocked_with_pr`` would silently rewrite
    to ``status: "merge_pending"``.
    """
    section = _step4c_section()
    assert '"pr": null,' in section
    assert '"pr_info":' in section


def test_step4d_reuse_path_has_verify_after_arm() -> None:
    """Step 4d item 3 ("Enable auto-merge") is the sole arm+verify site on

    the Pre-Stage Detector Guard reuse path (which skips Step 4c entirely).
    It must call the verify script after arming.
    """
    section = _step4d_enable_automerge_section()
    assert "prep_pr_finalize.py verify" in section
    assert "--require-automerge" in section


def test_step4d_verify_failure_emits_automerge_not_armed() -> None:
    section = _step4d_enable_automerge_section()
    assert "automerge_not_armed" in section


def test_finalize_regress_reasons_note_present_for_automerge_not_armed() -> None:
    content = _cmd("auto-dev-finalize.md")
    assert (
        "Do not add `automerge_not_armed` to `FINALIZE_REGRESS_BLOCKER_REASONS`"
        in content
    )


def test_headless_contract_documents_automerge_not_armed() -> None:
    content = _doc("headless-contract.md")

    gate_start = content.index("## 2. Gate-Collapse Table")
    gate_end = content.index("## 3. Structured Output")
    assert "automerge_not_armed" in content[gate_start:gate_end]

    reason_start = content.index("### 4.2")
    reason_end = content.index("#### Phase B fields")
    assert "automerge_not_armed" in content[reason_start:reason_end]
