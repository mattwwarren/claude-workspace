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


# --- #2581: arm pinned to the verified head SHA; fail-closed exit 2 ---

_UNDETERMINABLE = "cannot be determined (fail closed, #2581)"


def test_ship_it_step4_pins_arm_to_local_head() -> None:
    section = _ship_it_step4_section()
    assert "HEAD_SHA=$(git rev-parse HEAD)" in section
    assert '--repo-path "$REPO_ROOT" --head-sha "$HEAD_SHA"' in section
    assert section.index("HEAD_SHA=") < section.index("arm-automerge")


def test_step4c_self_heal_arm_pins_to_verify_head_sha() -> None:
    section = _step4c_section()
    assert (
        "arm-automerge <pr_number> --repo-path <worktree> --head-sha <head_sha>"
        in section
    )
    assert "`head_sha` field of the verify JSON" in section


def test_step4d_arm_and_retry_pin_to_worktree_head() -> None:
    section = _step4d_enable_automerge_section()
    assert "HEAD_SHA=$(git rev-parse HEAD)" in section
    assert section.count('--head-sha "$HEAD_SHA"') == 2


def test_sentinel_recovery_hint_carries_head_sha() -> None:
    section = _step4c_sentinel_section()
    assert "--repo-path <worktree> --head-sha <head-sha>" in section


def test_exit_2_documents_undeterminable_config() -> None:
    sentinel = _step4c_sentinel_section()
    variant_c = sentinel[sentinel.index("- (c) Invocation error") :]
    for section in (
        _ship_it_step4_section(),
        _step4c_section(),
        _step4d_enable_automerge_section(),
        variant_c,
    ):
        assert _UNDETERMINABLE in section


def test_headless_contract_documents_head_pin() -> None:
    content = _doc("headless-contract.md")
    assert "--head-sha" in content
    assert "--match-head-commit" in content


# --- #2625: classifier-denied arm ---
#
# The auto-mode permission classifier can deny the ``arm-automerge`` Bash call
# before ``prep_pr_finalize.py`` starts, so ``arm_automerge()`` never runs and a
# denied call has no exit status. The finalize prose must report that distinctly
# (variant (d) of ``automerge_not_armed``) instead of a generic or misleading
# sentinel, without retrying, working around, or merging directly.

_CANONICAL_MARKER_PREFIX = "<!-- Canonical #2625 marker: "


def _canonical_denied_arm_marker() -> str:
    content = _doc("headless-contract.md")
    start = content.index(_CANONICAL_MARKER_PREFIX) + len(_CANONICAL_MARKER_PREFIX)
    end = content.index(" -->", start)
    return content[start:end]


_DENIAL_PHRASE = (
    "Permission for this action was denied by the Claude Code auto mode classifier"
)
_DENIED_ARM_MARKER = _canonical_denied_arm_marker()
_ARM_COMMAND = "prep_pr_finalize.py arm-automerge"
_NOT_RETRIED = "not retried and not worked around"
_OPERATOR_SHELL = "from an operator shell"
_STANDING_RULE = "add an allow rule for that exact command in their own settings"
_EXCEPTION = "**Exception (#2625)"
_BY_HAND = (
    "prep_pr_finalize.py arm-automerge <pr-number> --repo-path <worktree> "
    "--head-sha <head-sha>"
)
_VERIFY_FIRST = (
    "run the verify gate first, to obtain `pr_number`, before applying the "
    "Base-branch-state classifier or the `agent_block` collapse"
)
_EXISTING_PATH = (
    "stays on the existing path (the `agent_block` collapse / the Tool-Use "
    "Denial Exit in `auto-dev.md`)"
)
_ORDER_OF_EVALUATION = (
    "Order of evaluation (#2625): (1) the Unavailability classifier, (2) this "
    "denied-arm check, (3) the Base-branch-state classifier, (4) the "
    "`HEADLESS BLOCK` → `agent_block` collapse"
)
_DENIED_OTHER_CALL = (
    "A denial of any other call (for example a denied `gh pr create` at Step 4c.2) "
)


def _variant_d() -> str:
    """Variant (d) of the ``automerge_not_armed`` template, and only that."""
    section = _step4c_sentinel_section()
    start = section.index("- (d) Classifier-denied arm")
    end = section.index("**Do not add `automerge_not_armed`", start)
    return section[start:end]


def _denied_arm_bullet() -> str:
    """The new Step 4c headless bullet, so older text cannot satisfy asserts."""
    section = _step4c_section()
    start = section.index("- **Classifier-denied arm (#2625)")
    end = section.index("- **Self-heal arm (#2576)", start)
    return section[start:end]


def _denial_exit_section() -> str:
    content = _cmd("auto-dev.md")
    start = content.index("## Tool-Use Denial Exit")
    end = content.index("## Park-comment stamp rule (#2135)", start)
    return content[start:end]


def test_sentinel_documents_classifier_denied_variant() -> None:
    variant = _variant_d()
    for literal in (
        _DENIED_ARM_MARKER,
        _DENIAL_PHRASE,
        "denied by the Claude Code auto mode classifier. Reason: <verbatim reason>",
        "not a gh failure, not an invocation error",
        _NOT_RETRIED,
        _BY_HAND,
        _OPERATOR_SHELL,
        _STANDING_RULE,
        (
            "auto-merge was not armed: the arm command was denied by the "
            "auto-mode permission classifier"
        ),
        "retry_eligible: true",
    ):
        assert literal in variant
    for forbidden in (
        "merge the PR directly",
        "bypassPermissions",
        "settings.json",
        "different tool",
        '"reason": "tool_denied"',
    ):
        assert forbidden not in variant


def test_denied_arm_marker_matches_the_canonical_contract() -> None:
    assert (
        _DENIED_ARM_MARKER
        == "arm-automerge blocked by the auto-mode permission classifier"
    )
    sites = (
        _cmd("auto-dev-finalize.md"),
        _appendix("finalize"),
        _cmd("auto-dev.md"),
        _cmd("ship-it.md"),
        _doc("headless-contract.md"),
    )
    assert all(_DENIED_ARM_MARKER in site for site in sites)


def test_denied_variant_keeps_automerge_not_armed_reason_and_pr_info() -> None:
    variant = _variant_d()
    for literal in (
        "the reason stays `automerge_not_armed`",
        "`pr_info`",
        "releases the row",
        "narrows the generic Tool-Use Denial Exit",
        "Step 4c.2 (#636)",
    ):
        assert literal in variant
    section = _step4c_sentinel_section()
    assert '"reason": "automerge_not_armed"' in section
    assert '"pr_info"' in section
    assert '"pr": null,' in section
    variant_b = section[section.index("- (b) ") : section.index("- (c) ")]
    assert "arm genuinely not attempted" in variant_b
    assert "auto-merge is not armed (no arm attempted: see details)" in variant_b


def test_step4c_denial_clause_precedes_invocation_error_branch() -> None:
    section = _step4c_section()
    assert section.index(_DENIED_ARM_MARKER) < section.index(
        "arm-automerge invocation error (exit <code>"
    )
    bullet = _denied_arm_bullet()
    assert "has no exit status" in bullet
    assert "never retried, routed around, or replaced by a direct `gh pr merge`" in (
        bullet
    )
    assert "**2 or any code other than 0/1/3** (an actual exit status:" in section


def test_step4c_detection_gate_selects_variant_d_only_with_pr_and_arm_context() -> None:
    bullet = _denied_arm_bullet()
    assert "Classifier-denied arm (#2625)" in bullet
    assert "inspect the `/prep-pr` subagent's returned text" in bullet
    assert _DENIAL_PHRASE in bullet
    assert _DENIED_ARM_MARKER in bullet
    assert (
        "only when `pr_number` is non-null AND the denied call is the arm command"
        in bullet
    )
    assert "names `arm-automerge`" in bullet
    assert "even when other checks failed" in bullet
    assert "also failed:" in bullet
    assert "SOLE failed check" in _step4c_section()


def test_step4c_denied_gh_pr_create_stays_on_existing_path() -> None:
    bullet = _denied_arm_bullet()
    assert _DENIED_OTHER_CALL + _EXISTING_PATH in bullet
    assert "A null `pr_number` is never variant (d)" in bullet


def test_denied_arm_check_ordering_is_stated() -> None:
    assert _ORDER_OF_EVALUATION in _step4c_section()
    content = _cmd("auto-dev-finalize.md")
    start = content.index("**Base-branch-state classifier (#2320):**")
    end = content.index("**Unavailability classifier", start)
    assert "denied-arm check (#2625)" in content[start:end]


def test_step4d_denial_emits_variant_d_not_retry() -> None:
    section = _step4d_enable_automerge_section()
    assert _DENIED_ARM_MARKER in section
    assert "after a classifier denial skip Retry" in section
    assert "Leave open / Abort" in section
    assert "variant (d)" in section
    assert "stage5_post_create" in section
    assert "has no exit status" in section
    assert "gh pr merge <pr-number> --auto --squash" not in section
    assert section.count('--head-sha "$HEAD_SHA"') == 2


def test_ship_it_step4_blocks_with_denied_marker() -> None:
    section = _ship_it_step4_section()
    assert "has no `arm_status`" in section
    assert f"BLOCK: {_DENIED_ARM_MARKER}" in section
    assert "<verbatim denial>" in section
    assert "No retry, no direct `gh pr merge`" in section
    assert _BARE_SHIP_IT_ARM not in section


def test_headless_contract_documents_denied_arm_variant() -> None:
    content = _doc("headless-contract.md")

    gate_start = content.index("## 2. Gate-Collapse Table")
    gate_end = content.index("## 3. Structured Output")
    assert _DENIED_ARM_MARKER in content[gate_start:gate_end]

    reason_start = content.index("### 4.2")
    reason_end = content.index("#### Phase B fields")
    assert _DENIED_ARM_MARKER in content[reason_start:reason_end]


def test_core_doc_delegates_variant_d_detail_to_appendix() -> None:
    core = _cmd("auto-dev-finalize.md")
    appendix = _appendix("finalize")
    assert _DENIED_ARM_MARKER in core
    assert "Step 4c re-verification failure: the `automerge_not_armed` sentinel" in core
    assert _OPERATOR_SHELL in appendix
    assert _STANDING_RULE in appendix
    assert _OPERATOR_SHELL not in core
    assert _STANDING_RULE not in core
    assert (
        "auto-merge was not armed: the arm command was denied by the "
        "auto-mode permission classifier"
    ) not in core


def test_denial_exit_carries_the_arm_automerge_exception() -> None:
    """Mirrors the #2135 ``cw signal-park`` exception pin in
    ``tests/test_sentinel_emission_discipline.py``."""
    denial = _denial_exit_section()
    for literal in (
        _EXCEPTION,
        _ARM_COMMAND,
        "automerge_not_armed",
        _DENIED_ARM_MARKER,
        "non-null AND the denied call is the arm command",
        "stays on this section's generic path",
        "Exception (#2135)",
    ):
        assert literal in denial
    assert denial.index(_EXCEPTION) < denial.index("**Action (headless).**")


def test_gate_collapse_row_carries_the_arm_automerge_parenthetical() -> None:
    row = next(
        line
        for line in _cmd("auto-dev.md").splitlines()
        if "Tool call denied by auto-mode classifier" in line
    )
    assert _ARM_COMMAND in row
    assert "Exception (#2625)" in row
    assert "cw signal-park" in row


def test_tool_denied_reason_row_carries_the_arm_automerge_note() -> None:
    row = next(
        line
        for line in _cmd("auto-dev.md").splitlines()
        if line.startswith("| `tool_denied` ")
    )
    assert _ARM_COMMAND in row
    assert "automerge_not_armed" in row


def test_step4c_denied_text_reaches_verify_gate_before_collapse() -> None:
    assert _VERIFY_FIRST in _denied_arm_bullet()
    section = _step4c_section()
    assert section.index(_VERIFY_FIRST) < section.index("Order of evaluation (#2625)")
