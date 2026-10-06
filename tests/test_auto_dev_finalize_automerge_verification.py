"""Guard tests: auto-merge arm-then-verify at both Step 4c and Step 4d (#1140).

Pure-markdown assertions over the auto-dev finalize pipeline instruction file
and the headless contract doc. This repo has an established convention (see
``tests/test_auto_dev_model_pins.py``, ``tests/test_auto_dev_preflight_resolutions.py``,
and ``tests/test_unavailability.py``) of reading ``.claude/commands/*.md``
prose and asserting substrings/regions. The reader itself is the shared
``_cmd()`` helper in ``tests/conftest.py`` (#1787) — it used to be a private
per-file copy here. ``_doc`` stays local.

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

from tests.conftest import _appendix, _cmd

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"


def _doc(name: str) -> str:
    return (DOCS / name).read_text()


def _step4c_section() -> str:
    content = _cmd("auto-dev-finalize.md")
    start = content.index("Main-session re-verification (do not skip):")
    end = content.index("### Step 4c.5")
    return content[start:end]


def _step4c_sentinel_section() -> str:
    """The ``automerge_not_armed`` sentinel template.

    #1879 relocated it to ``auto-dev-finalize-appendix.md``: the verify
    passing is the common path, so the failure sentinel is rare-path. The
    core doc keeps the verify invocation, the JSON-parse requirement, and
    both the interactive and headless dispositions.
    """
    content = _appendix("finalize")
    start = content.index("## Step 4c re-verification failure")
    end = content.index("\n## ", start)
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
    section = _step4c_sentinel_section()
    assert '"reason": "automerge_not_armed"' in section
    assert '"stage_reached": "stage5_post_create"' in section
    assert '"pr_info"' in section
    assert "the `automerge_not_armed` sentinel" in _step4c_section()


def test_step4c_automerge_not_armed_uses_pr_info_not_pr_object() -> None:
    """Regression guard for the pr/pr_info coercion trap (#1140).

    Within the new sentinel template block, ``pr`` must be explicitly null
    alongside a populated ``pr_info`` — never a populated ``pr`` object,
    which the parser's ``_coerce_blocked_with_pr`` would silently rewrite
    to ``status: "merge_pending"``.
    """
    section = _step4c_sentinel_section()
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
    content = _appendix("finalize")
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


# --- #2576: bounded-retry arm-automerge at every finalize arm site ---

_BARE_SHIP_IT_ARM = 'gh pr merge "$PR_NUMBER" --auto --squash'
_FINALIZE_RESOLVER = (
    "for candidate in .claude/scripts/prep_pr_finalize.py "
    'scripts/prep_pr_finalize.py "$HOME/.claude/scripts/prep_pr_finalize.py"'
)
_INVOCATION_ERROR = "arm-automerge invocation error (exit"


def _ship_it_step4_section() -> str:
    content = _cmd("ship-it.md")
    start = content.index("## Step 4: Enable auto-merge")
    end = content.index("## Step 5: Register PR monitor")
    return content[start:end]


def test_ship_it_step4_arms_through_arm_automerge() -> None:
    section = _ship_it_step4_section()
    assert '"$FINALIZE" arm-automerge "$PR_NUMBER" --repo-path "$REPO_ROOT"' in section
    assert _BARE_SHIP_IT_ARM not in section
    assert "arm-automerge invocation error (exit 2, not a gh failure)" in section


def test_step4d_arm_uses_arm_automerge_with_resolver() -> None:
    section = _step4d_enable_automerge_section()
    assert "arm-automerge <pr-number> --repo-path <worktree>" in section
    assert _FINALIZE_RESOLVER in section
    assert "gh pr merge <pr-number> --auto --squash" not in section
    assert _INVOCATION_ERROR in section


def test_step4d_ui_evidence_precondition_precedes_the_arm() -> None:
    """The UI Evidence Gate exemption must be read before the runnable arm.

    On a repo without branch protection, arming a held PR merges it at once, so
    the skip condition cannot trail the ``arm-automerge`` block.
    """
    section = _step4d_enable_automerge_section()
    precondition = (
        'Precondition: if the UI Evidence Gate above resolved to "Hold" '
        "(interactive) or fired in headless, skip this entire item"
    )
    assert precondition in section
    assert section.index(precondition) < section.index(_FINALIZE_RESOLVER)


def test_step4c_headless_self_heals_with_arm_automerge_and_resolver() -> None:
    """The reuse path skips Step 4c, so the resolver lives at both arm sites."""
    section = _step4c_section()
    assert "arm-automerge <pr_number> --repo-path <worktree>" in section
    assert _FINALIZE_RESOLVER in section
    assert _INVOCATION_ERROR in section
    assert "SOLE failed check" in section


def test_automerge_not_armed_sentinel_carries_gh_stderr() -> None:
    section = _step4c_sentinel_section()
    assert "gh stderr: <verbatim gh_stderr" in section
    assert "arm-automerge: attempts=<k>/<max>, gh exit <code>" in section
    assert "prep_pr_finalize.py arm-automerge <pr-number> --repo-path <worktree>" in (
        section
    )


def test_headless_contract_documents_arm_automerge() -> None:
    content = _doc("headless-contract.md")

    gate_start = content.index("## 2. Gate-Collapse Table")
    gate_end = content.index("## 3. Structured Output")
    assert "arm-automerge" in content[gate_start:gate_end]

    reason_start = content.index("### 4.2")
    reason_end = content.index("#### Phase B fields")
    assert "arm-automerge" in content[reason_start:reason_end]


def test_automerge_not_armed_sentinel_documents_non_arm_variants() -> None:
    """The retries-exhausted template is untrue on the no-arm and error paths."""
    section = _step4c_sentinel_section()
    assert "**Variants**" in section
    assert "auto-merge is not armed (no arm attempted: see details)" in section
    assert (
        "arm-automerge invocation error (exit <code>, not a gh failure): <stderr>"
        in section
    )
    assert "arm-automerge could not run (invocation error, not a gh failure)" in section


def test_ship_it_step4_bash_case_separates_exit_1_from_invocation_error() -> None:
    """The executable block, not just the prose, must carry the distinction."""
    section = _ship_it_step4_section()
    block = section[
        section.index("```bash") : section.index("```\n", section.index("```bash") + 7)
    ]
    assert (
        "arm-automerge invocation error (exit $arm_status, not a gh failure)" in block
    )
    assert (
        "failed after bounded retries for PR #$PR_NUMBER (arm-automerge exit 1)"
        in block
    )
