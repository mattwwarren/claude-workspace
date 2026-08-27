"""Tests for cw.review_findings — executor-neutral structured finding contract.

Covers the model group (Finding/EscalationMetadata/ReviewerFindingsDocument/
ReviewVerdict and friends), the validation/dedup/aggregation functions, the
escalation strip-on-invalid-evidence rule, and the #1108 artifact writer.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from cw.codex_review import _parse_unified_diff
from cw.review_findings import (
    _LINE_ANCHOR_TOLERANCE,
    _VALID_SEVERITIES,
    AcceptedFinding,
    CapturedDiff,
    Finding,
    RejectedFinding,
    RejectedFindingReason,
    ReviewerFindingsDocument,
    ReviewerRunFailure,
    ReviewerRunRecord,
    ReviewVerdict,
    StrippedEscalation,
    _anchor_in_enclosing_def,
    _best_effort_discarded_tally,
    _classify_finding,
    _content_rescue_anchor,
    _enclosing_def_span,
    _evidence_diff_pair,
    _evidence_in_claimed_lines,
    _evidence_removed_in_fix_diff,
    _line_exceeds_file_length,
    _line_reference_valid,
    _normalize_diff_text,
    _normalize_unicode_punctuation,
    _reconcile_evidence_window,
    _select_rejected_must_fix,
    _strip_diff_markers,
    consolidate_verdict,
    dedupe_findings,
    derive_review_counts,
    parse_reviewer_document,
    validate_reviewer_document,
    write_review_verdict,
)
from tests.conftest import (
    FindingKwargs,
    _doc_payload,
    _finding_kwargs,
    _make_debt_record,
    _make_diff,
    _make_escalation,
    _make_finding,
    _make_reviewer_doc,
    _without_evidence,
)

# -- #1738 fixtures: real #1729 diagnostics artifact -----------------------
#
# Redacted from the unrotated session diagnostics located at
# /home/matthew/.local/share/cw/sessions/dc7cf73e/diagnostics/
# cycle0-review-verdict.json (reviewed_sha b5c8119e... matches the real
# commit b5c8119e "chore(#1729): mark Stage 2 implementation complete"),
# per the fixtures-for-external-systems rule (real capture, not invented).
# The diff text below is verbatim `git diff 494414f8^ 494414f8 --
# tests/test_dispatch.py` output from this repo.

_PR1729_TEST_DISPATCH_DIFF = '''\
diff --git a/tests/test_dispatch.py b/tests/test_dispatch.py
index 21cc6981..043f6a49 100644
--- a/tests/test_dispatch.py
+++ b/tests/test_dispatch.py
@@ -9491,7 +9491,22 @@ class TestApplyStagedDecision:
         that can carry a non-null blocker (schema.py's #777 exception --
         'blocked'/'merge_gate_blocked' only) plus the _AWAITING_OPERATOR_REASON
         substitute Rule 5 writes when blocker_reason is in
-        OPERATOR_UNAVAILABLE_BLOCKER_REASONS.
+        OPERATOR_UNAVAILABLE_BLOCKER_REASONS, plus (#1729) the
+        "codex_must_fix_mechanically_rejected" substitute -- the one gate-class
+        park (#1714's _park_must_fix_mechanically_rejected) whose breadcrumbs
+        genuinely originate from a populated blocker dict rather than a
+        hardcoded breadcrumbs="" literal.
+
+        Membership in BREADCRUMB_ELIGIBLE_PAUSED_STATUSES does not by itself
+        cause a breadcrumb to be emitted: every producing _park_* helper must
+        independently stamp non-empty breadcrumbs content (the constant has no
+        runtime reader in src/ -- see the block comment above its definition
+        in routing.py). The exclusion assertions below prove the other
+        gate-class parks (review_health_gate, finalize_hold, signoff_gate,
+        approval_gate -- the last also covering scope_hint_gate, which reuses
+        approval_gate's paused_status literal) stay out of this set: they
+        hardcode breadcrumbs="", so adding their paused_status here would be
+        cosmetic, not a fix.
         """
         from cw.auto_dev_result import (
             OPERATOR_UNAVAILABLE_BLOCKER_REASONS,
@@ -9502,14 +9517,18 @@ class TestApplyStagedDecision:
             BREADCRUMB_ELIGIBLE_PAUSED_STATUSES,
         )

+        must_fix_mechanically_rejected = "codex_must_fix_mechanically_rejected"
+
         assert {
             "blocked",
             "merge_gate_blocked",
             "awaiting_operator_availability",
+            must_fix_mechanically_rejected,
         } == BREADCRUMB_ELIGIBLE_PAUSED_STATUSES
         # every non-substitute member is drawn from STAGE_FAILURE_STATUSES
         assert (
-            BREADCRUMB_ELIGIBLE_PAUSED_STATUSES - {_AWAITING_OPERATOR_REASON}
+            BREADCRUMB_ELIGIBLE_PAUSED_STATUSES
+            - {_AWAITING_OPERATOR_REASON, must_fix_mechanically_rejected}
         ) <= STAGE_FAILURE_STATUSES
         # scope_exceeded/forbidden_area excluded by design (#777: never carry a
         # blocker), not oversight
@@ -9520,6 +9539,18 @@ class TestApplyStagedDecision:
         # set is non-empty
         assert OPERATOR_UNAVAILABLE_BLOCKER_REASONS
         assert _AWAITING_OPERATOR_REASON in BREADCRUMB_ELIGIBLE_PAUSED_STATUSES
+        assert must_fix_mechanically_rejected in BREADCRUMB_ELIGIBLE_PAUSED_STATUSES
+
+        # gate-class exclusion (#1729): each of these hardcodes breadcrumbs=""
+        # at its _park_* call site (routing.py), so membership here would not
+        # change what gets emitted -- their paused_status must stay excluded.
+        for gate_paused_status in (
+            "review_health_gate",
+            "finalize_hold",
+            "signoff_gate",
+            "approval_gate",
+        ):
+            assert gate_paused_status not in BREADCRUMB_ELIGIBLE_PAUSED_STATUSES

     # -- review-health gate (#1702) --------------------------------------

'''

# The real Finding field values from the rejected #1729 SysAdmin Reviewer
# finding, quoted verbatim from the diagnostics artifact above.
_PR1729_REJECTED_FINDING_KWARGS: FindingKwargs = {
    "severity": "SHOULD_FIX",
    "file": "tests/test_dispatch.py",
    "line_start": 9522,
    "line_end": 9527,
    "summary": "The pinning test does not pin the duplicated monitor allowlist",
    "consequence": (
        "This assertion validates only the Python constant. A future edit can "
        "update `routing.py` without updating `_BLOCKER_REASON_PAUSED_STATUSES` "
        "in `attention_monitor.sh`, and the test will remain green while the "
        "attention stream silently omits blocker reasons—the same drift this "
        "change repairs."
    ),
    "suggested_fix": (
        "Add a synchronization assertion that reads and parses "
        "`_BLOCKER_REASON_PAUSED_STATUSES` from `attention_monitor.sh` and "
        "compares it with `BREADCRUMB_ELIGIBLE_PAUSED_STATUSES`, or generate "
        "both representations from one source."
    ),
    "evidence": (
        "assert {\n"
        '            "blocked",\n'
        '            "merge_gate_blocked",\n'
        '            "awaiting_operator_availability",\n'
        "            must_fix_mechanically_rejected,\n"
        "        } == BREADCRUMB_ELIGIBLE_PAUSED_STATUSES"
    ),
    "confidence": "HIGH",
    "escalation": None,
}

# -- #1764 fixture: reconstructed 9491 MUST_FIX finding ---------------------
#
# #1764 asks whether the whole-function structural-claim rejection mode
# (a MUST_FIX whose evidence describes an aggregate property of a function
# rather than quoting diff-resident text) survives the #1738/#1743 matcher
# changes. The genuine finding it investigates -- a MUST_FIX on
# tests/test_dispatch.py:9491 -- was mechanically rejected and reported via
# GitHub comment id 5226090232 (fetched live via
# `gh api repos/mattwwarren/claude-workspace/issues/comments/5226090232`,
# html_url
# https://github.com/mattwwarren/claude-workspace/issues/1729#issuecomment-5226090232),
# but (per #1763) no diagnostics artifact for it survived on this machine the
# way #1729's did (_PR1729_REJECTED_FINDING_KWARGS above) -- so only what the
# rendered comment itself carries is genuine.
#
# GENUINE (verbatim from the comment): severity, file, line_start, summary.
# The comment's rendered line is:
#   - **tests/test_dispatch.py:9491** — Split the expanded breadcrumb
#     composition test; it now exceeds the 50-line function threshold and
#     covers two independent contracts. (rejected: evidence_not_in_diff)
#
# RECONSTRUCTED, NOT RECOVERABLE (no surviving artifact has them; authored to
# faithfully reproduce the finding's shape -- a whole-function structural
# claim whose ``evidence`` is reviewer prose describing an aggregate property
# of the function (its line count, its number of contracts), never a verbatim
# quote of diff-resident text -- the exact defect class #1764 investigates):
# line_end, consequence, suggested_fix, evidence, confidence. The reused diff
# (``_pr1729_captured_diff`` below) is byte-identical to the diff the
# original #1729 reviewer pass saw for this file (`git diff
# 494414f8..b5c8119ec9e09b34756ff6f6b9f1b62c3fb23e64 -- tests/test_dispatch.py`
# is empty), and the real post-change function this finding targets is named
# ``test_breadcrumb_eligible_paused_statuses_composition`` (confirmed via
# `git show 494414f8:tests/test_dispatch.py`).
_PR1729_9491_MUST_FIX_FINDING_KWARGS: FindingKwargs = {
    "severity": "MUST_FIX",
    "file": "tests/test_dispatch.py",
    "line_start": 9491,
    "line_end": None,
    "summary": (
        "Split the expanded breadcrumb composition test; it now exceeds the "
        "50-line function threshold and covers two independent contracts."
    ),
    "consequence": (
        "A single test asserting two independent contracts (breadcrumb "
        "membership and gate-class exclusion) fails ambiguously — a future "
        "reader can't tell which contract broke without re-reading the "
        "whole body."
    ),
    "suggested_fix": (
        "Split into two tests, one per contract, each under the 50-line threshold."
    ),
    "evidence": (
        "test_breadcrumb_eligible_paused_statuses_composition now exceeds "
        "the 50-line function threshold and covers two independent "
        "contracts."
    ),
    "confidence": "HIGH",
    "escalation": None,
}


def _captured_diff_from_text(diff_text: str) -> CapturedDiff:
    """Build a real ``CapturedDiff`` from a captured unified-diff text via the
    unmodified diff parser.

    Uses :func:`cw.codex_review._parse_unified_diff` against the verbatim
    diff fixture (NOT ``_make_diff``, which never generates context lines) —
    the #1738 hunk-context-window tests need real context-line content.
    Shared by :func:`_pr1729_captured_diff`, :func:`_pr1703_captured_diff`,
    and :func:`_pr1784_captured_diff` (#1764).
    """
    file_diffs, file_line_text, file_window_text, _changed = _parse_unified_diff(
        diff_text
    )
    files = {f: sorted(lines) for f, lines in file_line_text.items()}
    return CapturedDiff(
        text=diff_text,
        files=files,
        file_diffs=file_diffs,
        file_line_text=file_line_text,
        file_window_text=file_window_text,
    )


def _pr1729_captured_diff() -> CapturedDiff:
    """Build the real #1729 ``CapturedDiff`` via the unmodified diff parser."""
    return _captured_diff_from_text(_PR1729_TEST_DISPATCH_DIFF)


# -- #1764 fixture: real #1703 diff (src/cw/prompts.py), corroboration -----
#
# Corroborating evidence for the same rejection mode (#1764) on an
# independent function: the ticket's own live #1703 reproduction rejected a
# structural finding on ``get_purpose_prompt`` (session bf5d88b3). Real diff
# captured via `git show 535fbd23713825eac75c22210c1b5d833c83a7cd --
# src/cw/prompts.py` (PR #1769, "feat(#1703): parameterize the quality-gate
# sentence in impl/debt prompts"), stripped of the commit-message preamble
# down to the first `diff --git` line. Built as a list of individually
# quoted line literals rather than one triple-quoted block -- several diff
# context lines are a bare " " (blank source line, single-space context
# marker), which a triple-quoted literal risks silently losing to
# editor/tool trailing-whitespace trimming (mirrors the
# _PR1784_INSTALL_SKILLS_DIFF_LINES rationale below).
_PR1703_PROMPTS_DIFF_LINES: list[str] = [
    "diff --git a/src/cw/prompts.py b/src/cw/prompts.py",
    "index e8e640b5..08ec002b 100644",
    "--- a/src/cw/prompts.py",
    "+++ b/src/cw/prompts.py",
    "@@ -2,6 +2,10 @@",
    " ",
    " from __future__ import annotations",
    " ",
    "+from dataclasses import dataclass",
    "+",
    "+from cw.models.enums import SessionPurpose",
    "+",
    ' CW_COMMAND_REFERENCE = """\\',
    " [cw commands]",
    " - cw dev-queue add <ticket> — enqueue a ticket for the auto-dev pipeline",
    "@@ -21,35 +25,73 @@ _AGENT_TEAM_GUIDANCE = (",
    '     "- Feed review findings back as follow-up work items."',
    " )",
    " ",
    "-PURPOSE_PROMPTS: dict[str, str] = {",
    '-    "impl": (',
    '-        "You are in the IMPLEMENTATION session. "',
    '-        "Write code, implement features, and fix bugs. "',
    '-        "If you notice quality issues (linting, types, duplication, docs), "',
    '-        "note them for later cleanup but stay focused on implementation. "',
    '+_DEFAULT_QUALITY_GATES = "ruff check, mypy, pytest"',
    "+",
    "+",
    "+def _quality_gate_sentence(commands: str) -> str:",
    '+    """Render the gate sentence naming *commands* as the gate list."""',
    "+    return (",
    '         "Before finishing any unit of work, run quality gates "',
    '-        "(ruff check, mypy, pytest) and fix all issues." + _AGENT_TEAM_GUIDANCE',
    '+        f"({commands}) and fix all issues."',
    "+    )",
    "+",
    "+",
    "+@dataclass(frozen=True)",
    "+class _PromptSpec:",
    "+    base: str",
    "+    gated: bool = False",
    "+",
    "+",
    "+def _render_prompt(spec: _PromptSpec, quality_gate_commands: str) -> str:",
    '+    """Render a prompt spec with the configured quality gate commands."""',
    "+    gate_sentence = (",
    "+        _quality_gate_sentence(quality_gate_commands)",
    "+        if spec.gated and quality_gate_commands",
    '+        else ""',
    "+    )",
    "+    return spec.base + gate_sentence + _AGENT_TEAM_GUIDANCE",
    "+",
    "+",
    "+_PROMPT_SPECS: dict[SessionPurpose, _PromptSpec] = {",
    "+    SessionPurpose.IMPL: _PromptSpec(",
    "+        base=(",
    '+            "You are in the IMPLEMENTATION session. "',
    '+            "Write code, implement features, and fix bugs. "',
    '+            "If you notice quality issues (linting, types, duplication, docs), "',
    '+            "note them for later cleanup but stay focused on implementation. "',
    "+        ),",
    "+        gated=True,",
    "     ),",
    '-    "idea": (',
    '-        "You are in the IDEA session. "',
    (
        '-        "Brainstorm approaches, '
        'explore design options, and prototype solutions. "'
    ),
    '-        "Think creatively about architecture and features. "',
    (
        '-        "Document ideas clearly for the '
        'implementation session to pick up.\\n\\n"'
    ),
    '-        "CRITICAL: Never clear context when exiting plan mode. "',
    '-        "Clearing context drops all delegation work on the floor. "',
    '-        "Always continue in the same context after plan approval."',
    "-        + _AGENT_TEAM_GUIDANCE",
    "+    SessionPurpose.IDEA: _PromptSpec(",
    "+        base=(",
    '+            "You are in the IDEA session. "',
    (
        '+            "Brainstorm approaches, '
        'explore design options, and prototype solutions. "'
    ),
    '+            "Think creatively about architecture and features. "',
    (
        '+            "Document ideas clearly for the '
        'implementation session to pick up.\\n\\n"'
    ),
    '+            "CRITICAL: Never clear context when exiting plan mode. "',
    '+            "Clearing context drops all delegation work on the floor. "',
    '+            "Always continue in the same context after plan approval."',
    "+        )",
    "     ),",
    '-    "debt": (',
    '-        "You are in the TECH DEBT session. "',
    (
        '-        "Fix linting violations, type errors, duplication, and '
        'documentation gaps. "'
    ),
    '-        "Do not implement new features or change behavior. "',
    '-        "Keep changes minimal and focused on quality. "',
    '-        "Before finishing any unit of work, run quality gates "',
    '-        "(ruff check, mypy, pytest) and fix all issues." + _AGENT_TEAM_GUIDANCE',
    "+    SessionPurpose.DEBT: _PromptSpec(",
    "+        base=(",
    '+            "You are in the TECH DEBT session. "',
    (
        '+            "Fix linting violations, type errors, duplication, and '
        'documentation gaps. "'
    ),
    '+            "Do not implement new features or change behavior. "',
    '+            "Keep changes minimal and focused on quality. "',
    "+        ),",
    "+        gated=True,",
    "     ),",
    " }",
    " ",
    "+PURPOSE_PROMPTS: dict[str, str] = {",
    "+    purpose.value: _render_prompt(",
    "+        spec=spec,",
    "+        quality_gate_commands=_DEFAULT_QUALITY_GATES,",
    "+    )",
    "+    for purpose, spec in _PROMPT_SPECS.items()",
    "+}",
    "+",
    " ",
    " def build_session_context(",
    "     client_name: str,",
    "@@ -77,6 +119,7 @@ def get_purpose_prompt(",
    "     *,",
    "     client_name: str | None = None,",
    "     workspace_path: str | None = None,",
    "+    quality_gate_commands: str | None = None,",
    " ) -> str | None:",
    '     """Resolve the system prompt for a given purpose.',
    " ",
    "@@ -87,6 +130,16 @@ def get_purpose_prompt(",
    "     prompt is prefixed with a ``[cw identity]`` block so the LLM knows",
    "     which client/purpose it belongs to.",
    " ",
    "+    *quality_gate_commands* replaces the gate list named in the ``impl`` and",
    "+    ``debt`` prompts, for clients whose stack is not the Python default:",
    "+",
    "+    - ``None`` (default): keep the default ``ruff check, mypy, pytest`` triad.",
    '+    - ``""``: omit the gate sentence entirely.',
    "+    - any other string: substitute it verbatim into the gate sentence.",
    "+",
    "+    It has no effect on ``idea`` (no gate sentence) and is superseded by a",
    "+    whole-prompt entry in *client_overrides*.",
    "+",
    "     Raises ValueError if only one of *client_name* / *workspace_path*",
    "     is provided.",
    '     """',
    "@@ -94,8 +147,18 @@ def get_purpose_prompt(",
    (
        '         msg = "client_name and workspace_path must both '
        'be provided or both omitted"'
    ),
    "         raise ValueError(msg)",
    " ",
    "+    try:",
    "+        prompt_spec = _PROMPT_SPECS.get(SessionPurpose(purpose))",
    "+    except ValueError:",
    "+        prompt_spec = None",
    "+",
    "     if client_overrides and purpose in client_overrides:",
    "         prompt: str | None = client_overrides[purpose]",
    (
        "+    elif prompt_spec and prompt_spec.gated and "
        "quality_gate_commands is not None:"
    ),
    "+        prompt = _render_prompt(",
    "+            spec=prompt_spec,",
    "+            quality_gate_commands=quality_gate_commands,",
    "+        )",
    "     else:",
    "         prompt = PURPOSE_PROMPTS.get(purpose)",
    " ",
]
_PR1703_PROMPTS_DIFF = "\n".join(_PR1703_PROMPTS_DIFF_LINES) + "\n"


def _pr1703_captured_diff() -> CapturedDiff:
    """Build the real #1703 ``CapturedDiff`` via the unmodified diff parser."""
    return _captured_diff_from_text(_PR1703_PROMPTS_DIFF)


# -- #1792 fixtures: real #1784 diff (scripts/install-skills.sh) -----------
#
# Real diff captured via `git diff 132e8fd6^ 132e8fd6 --
# scripts/install-skills.sh` -- this repo's own commit implementing #1784.
# No diagnostics artifact for the original mechanically-rejected reviewer
# finding survived on this machine (unlike #1729's), so -- unlike
# _PR1729_REJECTED_FINDING_KWARGS, which is quoted verbatim from one -- the
# Finding objects the tests below build are test-authored, not captured.
# The CapturedDiff substrate they're checked against is NOT invented,
# though: it's parsed by the real, unmodified _parse_unified_diff from this
# real merged diff, so the added-line vs hunk-context-line split the tests
# rely on (verified below: lines 235-243 are diff-added, 244-245 are
# unchanged hunk-context) is genuine, not constructed by hand the way
# _make_diff's always-equal file_line_text/file_window_text would be.
# Built via "\n".join([...]) of individual repr'd line literals, NOT a
# triple-quoted block: several diff context lines are a bare " " (blank
# source line, single-space context marker), which a triple-quoted literal
# risks silently losing to editor/tool trailing-whitespace trimming --
# byte-fidelity to the real diff matters here (the added-line/hunk-context
# split several tests below depend on), so each line is its own quoted
# string, immune to end-of-line trimming.
_PR1784_INSTALL_SKILLS_DIFF_LINES: list[str] = [
    "diff --git a/scripts/install-skills.sh b/scripts/install-skills.sh",
    "index d7207ec0..a39dee95 100755",
    "--- a/scripts/install-skills.sh",
    "+++ b/scripts/install-skills.sh",
    "@@ -18,12 +18,16 @@",
    " #   above still holds — an agent that exists only in global-claude never enters",
    " #   this manifest, so it is never removed by cw.",
    " #",
    "-#   Overwrite hazard: `cp` here is unconditional, with no diff/staleness check",
    "-#   against the destination.  If an agent is hand-edited directly in",
    "-#   global-claude (the canonical source) after this repo's .claude/agents/",
    "-#   copy was last refreshed, the next install run silently clobbers that edit",
    "-#   back to the stale cw copy.  Re-import from global-claude into this repo's",
    "-#   .claude/agents/ before running install if you've been editing there.",
    "+#   Overwrite safety (#1784): a baseline shadow-copy store at",
    "+#   ~/.claude/.cw-agents-baseline/ records the exact content cw itself last",
    "+#   wrote for each installed agent.  On each run, before copying an agent",
    "+#   file: if the destination doesn't exist yet, or matches the source, or",
    "+#   matches the recorded baseline, the copy proceeds normally (this also",
    '+#   covers the ordinary "cw\'s source legitimately changed" case with zero',
    "+#   added friction).  Otherwise the destination has been hand-edited (or has",
    "+#   unknown provenance, e.g. no baseline was ever recorded) and the script",
    "+#   refuses to overwrite it, printing the source/destination paths and",
    "+#   exiting non-zero.  Pass --force to overwrite anyway.",
    " #",
    " # PORTABILITY:",
    " #   Targets bash 3.2 (macOS /bin/bash) as well as modern bash on Linux.  Do not",
    "@@ -32,6 +36,20 @@",
    ' #   `set -u` errors on a bare "${arr[@]}" when the array is empty under 3.2.',
    " set -euo pipefail",
    " ",
    "+# --force bypasses the agent overwrite-safety check below (#1784), matching",
    "+# the repo-wide --force convention (cli/sessions.py, cli/spawn.py,",
    "+# worktree_gc.py). No other flags are accepted.",
    "+FORCE=0",
    '+for arg in "$@"; do',
    '+    case "$arg" in',
    "+        --force) FORCE=1 ;;",
    "+        *)",
    '+            echo "Error: unknown argument: $arg" >&2',
    "+            exit 1",
    "+            ;;",
    "+    esac",
    "+done",
    "+",
    ' SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
    ' PROJECT_DIR="$(dirname "$SCRIPT_DIR")"',
    " ",
    '@@ -86,6 +104,15 @@ SKILLS_DST="$CLAUDE_HOME/skills"',
    ' AGENTS_DST="$CLAUDE_HOME/agents"',
    ' MANIFEST="$CLAUDE_HOME/.cw-skills-manifest"',
    " ",
    "+# Baseline shadow-copy store for agent overwrite-safety (#1784): one copy per",
    "+# installed agent file, holding the exact content cw itself last wrote there.",
    "+# This is what lets the agent-copy loop below tell \"cw's own source"
    " legitimately",
    '+# changed" apart from "something else edited the destination directly" —',
    "+# mtime doesn't work (plain cp resets mtime to now on every write), and git",
    "+# status doesn't work either (a normal cw install intentionally leaves",
    "+# global-claude's copy uncommitted, per the NOTE ON AGENTS above).",
    '+AGENTS_BASELINE_DIR="$CLAUDE_HOME/.cw-agents-baseline"',
    "+",
    " # ---------------------------------------------------------------------------",
    " # 1. Validate source directories",
    " # ---------------------------------------------------------------------------",
    "@@ -96,7 +123,55 @@ fi",
    " ",
    " # -p is a no-op when the path is an existing dir OR a symlink to one, so this is",
    " # safe for the agents symlink-into-global-claude layout described above.",
    '-mkdir -p "$COMMANDS_DST" "$SKILLS_DST" "$AGENTS_DST"',
    '+mkdir -p "$COMMANDS_DST" "$SKILLS_DST" "$AGENTS_DST" "$AGENTS_BASELINE_DIR"',
    "+",
    "+# _agent_conflict_reason <src_file> <dst_file> <baseline_file>",
    "+# Echoes a reason and returns 0 if installing src_file over dst_file would",
    "+# clobber a change cw did not itself make. Returns 1 (safe to install) when",
    "+# dst_file doesn't exist yet, is byte-identical to src_file, or is",
    "+# byte-identical to the recorded baseline (i.e. nothing has touched it since",
    "+# cw last wrote it there).",
    "+_agent_conflict_reason() {",
    '+    local src_file="$1"',
    '+    local dst_file="$2"',
    '+    local baseline_file="$3"',
    "+",
    '+    if [ ! -e "$dst_file" ]; then',
    "+        return 1",
    "+    fi",
    '+    if cmp -s "$src_file" "$dst_file"; then',
    "+        return 1",
    "+    fi",
    '+    if [ -f "$baseline_file" ] && cmp -s "$baseline_file" "$dst_file"; then',
    "+        return 1",
    "+    fi",
    '+    if [ ! -f "$baseline_file" ]; then',
    "+        echo \"destination differs from cw's source and cw has no record of"
    ' installing it (no baseline on file)"',
    "+        return 0",
    "+    fi",
    "+    echo \"destination differs from cw's source and from the last copy cw"
    ' installed — something other than cw modified it"',
    "+    return 0",
    "+}",
    "+",
    "+# _print_agent_conflict <name> <src_file> <dst_file> <reason>",
    "+_print_agent_conflict() {",
    '+    local name="$1"',
    '+    local src_file="$2"',
    '+    local dst_file="$3"',
    '+    local reason="$4"',
    "+",
    "+    {",
    '+        echo "ERROR: refusing to overwrite a modified agent spec."',
    '+        echo ""',
    '+        echo "  agent:               $name"',
    '+        echo "  cw source:           $src_file"',
    '+        echo "  install destination: $dst_file"',
    '+        echo "  reason:              $reason"',
    '+        echo ""',
    '+        echo "  To keep those changes:  re-import them into claude-workspace,'
    ' commit, and re-run."',
    '+        echo "  To discard and overwrite: ./scripts/install-skills.sh --force"',
    "+    } >&2",
    "+}",
    " ",
    " # ---------------------------------------------------------------------------",
    " # 2. Build the NEW manifest (what this run will install)",
    "@@ -119,6 +194,7 @@ done",
    " ",
    " agent_count=0",
    " excluded_agent_count=0",
    "+agent_conflicts=()",
    ' if [ -d "$AGENTS_SRC" ]; then',
    '     for src_file in "$AGENTS_SRC"/*.md; do',
    '         [ -f "$src_file" ] || continue',
    '@@ -127,12 +203,44 @@ if [ -d "$AGENTS_SRC" ]; then',
    "             excluded_agent_count=$((excluded_agent_count + 1))",
    "             continue",
    "         fi",
    '-        cp "$src_file" "$AGENTS_DST/$name"',
    "+",
    '+        dst_file="$AGENTS_DST/$name"',
    '+        baseline_file="$AGENTS_BASELINE_DIR/$name"',
    "+",
    "+        # See _agent_conflict_reason above. Skipped entirely when --force is",
    "+        # passed. The `if` condition (rather than a plain `reason=$(...)`",
    "+        # assignment) is deliberate: under `set -e`, a bare assignment from a",
    '+        # command substitution that returns non-zero (the "safe" case here)',
    "+        # would abort the whole script.",
    '+        if [ "$FORCE" -eq 0 ] && reason="$(_agent_conflict_reason "$src_file"'
    ' "$dst_file" "$baseline_file")"; then',
    '+            _print_agent_conflict "$name" "$src_file" "$dst_file" "$reason"',
    '+            agent_conflicts+=("$name")',
    "+            continue",
    "+        fi",
    "+",
    '+        cp -p "$src_file" "$dst_file"',
    '+        cp -p "$src_file" "$baseline_file"',
    '         new_entries+=("agents/$name")',
    "         agent_count=$((agent_count + 1))",
    "     done",
    " fi",
    " ",
    "+# Deferred abort: must happen here, before the skills loop, the manifest",
    "+# write, and (critically) the prune step below. A conflicting agent is",
    "+# deliberately withheld from new_entries above so its destination is left",
    "+# untouched — but the prune step treats any old-manifest entry absent from",
    "+# new_entries as an orphan and deletes it. Continuing past this point with a",
    "+# conflict pending would make this run's own prune logic delete the very",
    "+# hand-edited file this feature exists to protect.",
    '+if [ "${#agent_conflicts[@]}" -gt 0 ]; then',
    '+    echo "" >&2',
    '+    echo "ERROR: ${#agent_conflicts[@]} agent(s) have hand-edited destinations'
    ' and were not installed:" >&2',
    '+    for conflicted_name in "${agent_conflicts[@]}"; do',
    '+        echo "  - $conflicted_name" >&2',
    "+    done",
    "+    exit 1",
    "+fi",
    "+",
    " skill_count=0",
    ' if [ -d "$SKILLS_SRC" ]; then',
    '     for src_dir in "$SKILLS_SRC"/*/; do',
    '@@ -205,6 +313,14 @@ if [ -f "$MANIFEST" ]; then',
    '                 rm -rf "$target"',
    "                 prune_count=$((prune_count + 1))",
    '                 pruned_names+=("$old_entry")',
    "+                # A pruned agent's baseline entry must go too, so a future",
    "+                # re-add under the same filename doesn't inherit stale",
    "+                # baseline state left over from before it was removed (#1784).",
    '+                case "$old_entry" in',
    "+                    agents/*)",
    '+                        rm -f "$AGENTS_BASELINE_DIR/${old_entry#agents/}"',
    "+                        ;;",
    "+                esac",
    "             fi",
    "         fi",
    '     done < "$MANIFEST"',
]
_PR1784_INSTALL_SKILLS_DIFF = "\n".join(_PR1784_INSTALL_SKILLS_DIFF_LINES) + "\n"


def _pr1784_captured_diff() -> CapturedDiff:
    """Build the real #1784 ``scripts/install-skills.sh`` ``CapturedDiff`` via
    the unmodified diff parser.
    """
    return _captured_diff_from_text(_PR1784_INSTALL_SKILLS_DIFF)


# Real content of scripts/install-skills.sh's post-#1784 new-file lines
# 235-245 (verified: `git show 132e8fd6:scripts/install-skills.sh | sed -n
# '235,245p'`, and cross-checked against _parse_unified_diff's own output).
# Lines 235-243 are diff-added; 244-245 are unchanged hunk-context -- the
# exact dual-substrate shape #1792 exists to reconcile. 11 lines total.
_PR1784_EVIDENCE_11_LINES = (
    'if [ "${#agent_conflicts[@]}" -gt 0 ]; then\n'
    '    echo "" >&2\n'
    '    echo "ERROR: ${#agent_conflicts[@]} agent(s) have hand-edited '
    'destinations and were not installed:" >&2\n'
    '    for conflicted_name in "${agent_conflicts[@]}"; do\n'
    '        echo "  - $conflicted_name" >&2\n'
    "    done\n"
    "    exit 1\n"
    "fi\n"
    "\n"
    "skill_count=0\n"
    'if [ -d "$SKILLS_SRC" ]; then'
)

# Fabricated 11-line evidence guaranteed absent from the #1784 diff at any
# offset -- the negative control for "evidence genuinely absent" (AC2/AC5),
# distinct from _PR1784_EVIDENCE_11_LINES's genuinely-real-but-undersized-
# window case. Line count matches (11) so detail-message assertions can pin
# on the same number in both the positive and negative fixtures.
_PR1784_ABSENT_EVIDENCE = (
    "some fabricated line one\n"
    "some fabricated line two\n"
    "some fabricated line three\n"
    "some fabricated line four\n"
    "some fabricated line five\n"
    "some fabricated line six\n"
    "some fabricated line seven\n"
    "some fabricated line eight\n"
    "some fabricated line nine\n"
    "some fabricated line ten\n"
    "some fabricated line eleven"
)


# -- #1976 fixtures: #1879-shape formatting-trivia false-rejects ----------
#
# Hand-authored (not a redacted external capture): unified diff is this
# repo's own format and the SHAPE is what is under test, not the literal
# bytes of #1879 -- see the plan's Adopted Assumptions. Parsed through the
# real _parse_unified_diff via _captured_diff_from_text, same as the #1729/
# #1784 fixtures above, so the line-number arithmetic is the production
# parser's, not the test's.
#
# New-file line map produced by that parse:
#   10 context  def render(value: str) -> str:
#   11 context      """Render *value* for the operator."""
#      removed      prefix = "legacy"       <- only in file_diffs
#   12 ADDED        prefix = "modern"  # rewritten -- see the ticket
#   13 context      suffix = "done"
#   14 context      return prefix + value + suffix
_ISSUE1879_DIFF_LINES = [
    "diff --git a/src/cw/example_renderer.py b/src/cw/example_renderer.py",
    "--- a/src/cw/example_renderer.py",
    "+++ b/src/cw/example_renderer.py",
    "@@ -10,5 +10,5 @@ def render(value: str) -> str:",
    " def render(value: str) -> str:",
    '     """Render *value* for the operator."""',
    '-    prefix = "legacy"',
    '+    prefix = "modern"  # rewritten -- see the ticket',
    '     suffix = "done"',
    "     return prefix + value + suffix",
]
_ISSUE1879_DIFF = "\n".join(_ISSUE1879_DIFF_LINES) + "\n"
_ISSUE1879_FILE = "src/cw/example_renderer.py"
_ISSUE1879_ADDED_LINE_NO = 12
_ISSUE1879_ADDED_LINE = '    prefix = "modern"  # rewritten -- see the ticket'
_ISSUE1879_REMOVED_LINE = '    prefix = "legacy"'
# The reviewer-quoted `-`/`+` pair for the single rewritten line: the removed
# half exists ONLY in file_diffs (a removed line has no new-file line number,
# so it is absent from both file_line_text and file_window_text).
_ISSUE1879_DIFF_PAIR_EVIDENCE = f"-{_ISSUE1879_REMOVED_LINE}\n+{_ISSUE1879_ADDED_LINE}"


# Unicode punctuation an LLM reviewer substitutes for its ASCII equivalent
# when quoting a diff (#1976). Built via chr() rather than written literally:
# ruff's RUF001 flags several of these as ambiguous inside a string literal,
# and an escape-vs-character mixture in the fixtures would be its own trap.
_EM_DASH = chr(0x2014)
_EN_DASH = chr(0x2013)
_LSQUO = chr(0x2018)
_RSQUO = chr(0x2019)
_LDQUO = chr(0x201C)
_RDQUO = chr(0x201D)
_NBSP = chr(0x00A0)

# The three verbatim normalization/rescue-stage suffixes #1976 appends to a
# rejected finding's `detail`, restated here as literals so the assertions
# below lock the exact operator-facing wording rather than re-deriving it.
_RESCUE_ATTEMPTED_DIAGNOSIS = (
    "; normalization applied: diff-marker stripping, unicode punctuation "
    "normalization (em/en dash, curly quotes, NBSP); diff-pair rescue: "
    "attempted (evidence is a -/+ line pair against a 1-line declared range) "
    "but no match in the file's raw diff text either"
)
_RESCUE_NOT_ATTEMPTED_DIAGNOSIS = (
    "; normalization applied: diff-marker stripping, unicode punctuation "
    "normalization (em/en dash, curly quotes, NBSP); diff-pair rescue: not "
    "attempted (evidence is not a -/+ line pair against a 1-line declared "
    "range); only the marker-strip and unicode-punctuation normalization "
    "above were applied"
)
_RESCUE_NOT_APPLICABLE_DIAGNOSIS = (
    "; normalization applied: diff-marker stripping, unicode punctuation "
    "normalization (em/en dash, curly quotes, NBSP); diff-pair rescue: not "
    "applicable (file-level fallback has no line anchor for a diff-pair "
    "rescue to resolve against)"
)
# #2019: the evidence_not_in_diff detail message's exact wording for the
# unbounded content-rescue miss, inserted before the pre-existing
# normalization-diagnosis suffix above. Only the line-anchored branch of
# _evidence_window_discrepancy_detail carries this clause -- the file-level
# (no line anchor) branch is unaffected by #2019, which only touches the
# line-anchored evidence-gate.
_UNBOUNDED_RESCUE_MISS_DIAGNOSIS = (
    "; an unbounded content-based re-anchoring search of the file's diff "
    "also found no match (#2019)"
)


def _issue1879_captured_diff() -> CapturedDiff:
    """Build the #1976/#1879-shape ``CapturedDiff`` via the real diff parser."""
    return _captured_diff_from_text(_ISSUE1879_DIFF)


class TestSeverityAndDispositionLiterals:
    def test_valid_severities_round_trip(self) -> None:
        for sev in ("MUST_FIX", "SHOULD_FIX", "DEBT", "NIT", "PRINCIPLE"):
            f = _make_finding(severity=sev)
            assert f.severity == sev

    def test_invalid_severity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_finding(severity="CRITICAL")

    def test_valid_dispositions_round_trip(self) -> None:
        # "dropped" (#1805) is the state for an accepted finding with no
        # adjudication decision, or a "fixed" claim that failed diff
        # verification -- stamped only by cw.review_adjudication.
        for disp in ("fixed", "rejected", "deferred", "dropped"):
            af = AcceptedFinding(
                finding=_make_finding(), reviewers=["r"], disposition=disp
            )
            assert af.disposition == disp

    def test_disposition_detail_defaults_blank(self) -> None:
        af = AcceptedFinding(finding=_make_finding(), reviewers=["r"])
        assert af.disposition_detail == ""

    def test_unmatched_adjudication_count_defaults_zero(self) -> None:
        verdict = consolidate_verdict([], _make_diff(), "abc1234")
        assert verdict.unmatched_adjudication_count == 0

    def test_invalid_disposition_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AcceptedFinding.model_validate(
                {
                    "finding": _make_finding(),
                    "reviewers": ["r"],
                    "disposition": "wontfix",
                }
            )


class TestRejectionReasonLiteral:
    def test_escalation_reason_is_valid_rejection_reason(self) -> None:
        # StrippedEscalation.reason accepts the escalation-only value.
        se = StrippedEscalation(
            reviewer_role="Security Reviewer",
            finding_index=0,
            target_reviewer="Performance Reviewer",
            reason="escalation_evidence_not_in_diff",
        )
        assert se.reason == "escalation_evidence_not_in_diff"

    def test_rejected_finding_never_uses_escalation_reason(self) -> None:
        # The escalation-only value is never produced on a RejectedFinding by
        # validate_reviewer_document — every rejected finding uses one of the
        # six core reasons.
        bad = Finding.model_construct(**_finding_kwargs(severity="BOGUS"))
        diff = _make_diff()
        _accepted, rejected, _stripped = validate_reviewer_document(
            _make_reviewer_doc(bad), diff
        )
        assert rejected
        escalation_only_reason: str = "escalation_evidence_not_in_diff"
        assert all(r.reason != escalation_only_reason for r in rejected)

    def test_rejected_finding_reason_rejects_escalation_value(self) -> None:
        # R6 (#1236): RejectedFinding.reason uses the split RejectedFindingReason
        # Literal, which excludes the escalation-only value.
        with pytest.raises(ValidationError):
            RejectedFinding.model_validate(
                {
                    "raw": {},
                    "reviewer_role": "R",
                    "reason": "escalation_evidence_not_in_diff",
                }
            )

    def test_stripped_escalation_reason_rejects_core_value(self) -> None:
        # R6 (#1236): StrippedEscalation.reason uses EscalationStripReason, which
        # excludes every core rejection reason.
        with pytest.raises(ValidationError):
            StrippedEscalation.model_validate(
                {
                    "reviewer_role": "R",
                    "finding_index": 0,
                    "target_reviewer": "Perf Reviewer",
                    "reason": "evidence_not_in_diff",
                }
            )

    def test_unanchored_is_valid_rejection_reason_literal(self) -> None:
        # #1632: "unanchored" is a 6th RejectedFindingReason value. Normal
        # operation never constructs a RejectedFinding with it (validate_
        # reviewer_document routes it to accepted instead) — this only pins
        # the Literal itself accepts direct construction.
        rf = RejectedFinding(raw={}, reviewer_role="R", reason="unanchored")
        assert rf.reason == "unanchored"

    def test_line_reference_out_of_range_is_valid_rejection_reason_literal(
        self,
    ) -> None:
        # #2007: a 7th RejectedFindingReason value, split off the generic
        # "invalid_line_reference" for the citation that names a line the file
        # does not have at all — a distinct producer defect from a citation
        # that merely drifted off its true position.
        rf = RejectedFinding(
            raw={}, reviewer_role="R", reason="line_reference_out_of_range"
        )
        assert rf.reason == "line_reference_out_of_range"

    def test_schema_invalid_is_valid_rejection_reason_literal(self) -> None:
        # #2029: an 8th RejectedFindingReason value, and the only one produced
        # BEFORE _classify_finding ever runs — at parse time, by
        # parse_reviewer_document, for a findings[] item that could not become
        # a Finding at all.
        assert "schema_invalid" in get_args(RejectedFindingReason)
        rf = RejectedFinding(raw={}, reviewer_role="R", reason="schema_invalid")
        assert rf.reason == "schema_invalid"


def _invalid_finding_payload(**overrides: object) -> dict[str, Any]:
    """A raw finding dict with ``evidence`` removed — schema-invalid (#2029)."""
    return _without_evidence(dict(_finding_kwargs(**overrides)))


class TestParseReviewerDocument:
    """#2029: one schema-invalid finding must not delete its siblings."""

    def test_siblings_survive_one_invalid_finding(self) -> None:
        payload = _doc_payload(
            dict(_finding_kwargs(summary="first")),
            _invalid_finding_payload(severity="NIT", summary="broken"),
            dict(_finding_kwargs(severity="DEBT", summary="last")),
        )
        doc, rejected = parse_reviewer_document(payload)

        assert [f.summary for f in doc.findings] == ["first", "last"]
        assert len(rejected) == 1
        rf = rejected[0]
        assert rf.reason == "schema_invalid"
        assert rf.reviewer_role == "Code Quality Reviewer"
        assert rf.raw == _invalid_finding_payload(severity="NIT", summary="broken")
        assert "evidence" in rf.detail

    def test_ac4_one_invalid_finding_plus_one_of_every_severity(self) -> None:
        severities = ["MUST_FIX", "SHOULD_FIX", "DEBT", "NIT", "PRINCIPLE"]
        payload = _doc_payload(
            _invalid_finding_payload(),
            *(dict(_finding_kwargs(severity=s)) for s in severities),
        )
        doc, rejected = parse_reviewer_document(payload)

        assert [f.severity for f in doc.findings] == severities
        assert len(rejected) == 1
        assert rejected[0].reason == "schema_invalid"

    def test_schema_invalid_must_fix_feeds_the_existing_1714_gate(self) -> None:
        # The design decision this ticket rests on: a schema-invalid finding is
        # an ordinary RejectedFinding, so #1714's force-block selector fires for
        # it with no new gating code at all.
        payload = _doc_payload(
            _invalid_finding_payload(severity="MUST_FIX"),
            dict(_finding_kwargs(severity="NIT")),
        )
        _doc, rejected = parse_reviewer_document(payload)

        assert rejected[0].raw["severity"] == "MUST_FIX"
        assert _select_rejected_must_fix(rejected) == rejected

    def test_non_list_findings_key_is_not_rescued(self) -> None:
        # The boundary: per-ITEM rescue only. A malformed `findings` key itself
        # is a structural failure and still propagates.
        payload = _doc_payload(findings="not a list at all")
        with pytest.raises(ValidationError):
            parse_reviewer_document(payload)

    def test_failed_reviewer_with_every_finding_invalid_parses_empty(self) -> None:
        payload = _doc_payload(
            _invalid_finding_payload(),
            _invalid_finding_payload(severity="NIT"),
            status="failed",
            detail="reviewer produced nothing usable",
        )
        doc, rejected = parse_reviewer_document(payload)

        assert doc.status == "failed"
        assert doc.findings == []
        assert len(rejected) == 2

    def test_non_dict_finding_item_is_rescued_not_crashed_on(self) -> None:
        payload = _doc_payload(
            detail="checked the diff; one item was unusable",
            findings=["not a finding at all"],
        )
        doc, rejected = parse_reviewer_document(payload)

        assert doc.findings == []
        assert len(rejected) == 1
        assert rejected[0].reason == "schema_invalid"
        assert rejected[0].raw == {"value": "not a finding at all"}

    def test_valid_document_reports_no_rejects(self) -> None:
        payload = _doc_payload(dict(_finding_kwargs()))
        doc, rejected = parse_reviewer_document(payload)

        assert len(doc.findings) == 1
        assert rejected == []

    def test_non_dict_payload_still_raises(self) -> None:
        # A --documents-from file holding a bare JSON array must still fail as a
        # structural error, not crash on a `.get()` against a list.
        with pytest.raises(ValidationError):
            parse_reviewer_document([1, 2, 3])

    def test_each_rejection_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        payload = _doc_payload(_invalid_finding_payload(), dict(_finding_kwargs()))
        with caplog.at_level(logging.INFO, logger="cw.review_findings._document"):
            parse_reviewer_document(payload)
        assert "schema_invalid" in caplog.text


class TestFindingValidation:
    def test_required_fields(self) -> None:
        f = _make_finding()
        assert f.file == "src/cw/foo.py"
        assert f.escalation is None

    def test_blank_evidence_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_finding(evidence="   ")

    def test_blank_summary_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_finding(summary="")

    def test_line_end_before_line_start_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_finding(line_start=10, line_end=5)

    def test_line_end_equal_line_start_ok(self) -> None:
        f = _make_finding(line_start=10, line_end=10)
        assert f.line_end == 10

    def test_file_level_finding_null_lines_ok(self) -> None:
        f = _make_finding(line_start=None, line_end=None)
        assert f.line_start is None


class TestEscalationMetadata:
    def test_required_fields_round_trip(self) -> None:
        e = _make_escalation()
        assert e.target_reviewer
        assert e.evidence_quote

    def test_blank_target_reviewer_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_escalation(target_reviewer="  ")

    def test_blank_evidence_quote_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_escalation(evidence_quote="")

    def test_finding_escalation_defaults_none(self) -> None:
        assert _make_finding().escalation is None

    def test_finding_round_trips_escalation(self) -> None:
        esc = _make_escalation(target_reviewer="Perf Reviewer")
        f = _make_finding(escalation=esc)
        assert f.escalation is not None
        assert f.escalation.target_reviewer == "Perf Reviewer"


class TestReviewerFindingsDocument:
    def test_failed_status_with_findings_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReviewerFindingsDocument(
                reviewer_role="R",
                status="failed",
                detail="crashed",
                findings=[_make_finding()],
            )

    def test_degraded_status_may_carry_findings(self) -> None:
        doc = ReviewerFindingsDocument(
            reviewer_role="R",
            status="degraded",
            detail="partial",
            findings=[_make_finding()],
        )
        assert len(doc.findings) == 1

    def test_ok_clean_review_round_trips(self) -> None:
        doc = ReviewerFindingsDocument(
            reviewer_role="R", status="ok", detail="checked X; no issues.", findings=[]
        )
        assert doc.findings == []

    def test_make_reviewer_doc_default_still_valid(self) -> None:
        # Tripwire for the conftest fixture-default fix (#1544): the zero-arg
        # _make_reviewer_doc() call (status="ok", findings=[]) must keep
        # validating cleanly once the new ok/empty-findings justification
        # validator lands — if the conftest default regresses to a blank
        # detail, this fails immediately instead of scattering failures
        # across the ~56 other call sites that rely on it.
        doc = _make_reviewer_doc()
        assert doc.status == "ok"
        assert doc.findings == []


class TestReviewerFindingsDocumentOkJustification:
    """R6 (#1544): status='ok' + empty findings requires a non-blank detail."""

    def test_ok_empty_findings_blank_detail_rejected(self) -> None:
        with pytest.raises(ValidationError, match="degraded"):
            ReviewerFindingsDocument(
                reviewer_role="R", status="ok", detail="", findings=[]
            )

    def test_ok_empty_findings_whitespace_detail_rejected(self) -> None:
        # Proves _is_blank's .strip() semantics are actually invoked, not a
        # naive falsy/empty-string check.
        with pytest.raises(ValidationError, match="degraded"):
            ReviewerFindingsDocument(
                reviewer_role="R", status="ok", detail="   ", findings=[]
            )

    def test_ok_empty_findings_nonblank_detail_passes(self) -> None:
        doc = ReviewerFindingsDocument(
            reviewer_role="R",
            status="ok",
            detail="Checked X, Y, Z; no issues.",
            findings=[],
        )
        assert doc.detail == "Checked X, Y, Z; no issues."

    def test_ok_nonempty_findings_blank_detail_passes(self) -> None:
        # The justification rule only applies when findings is empty.
        doc = ReviewerFindingsDocument(
            reviewer_role="R", status="ok", detail="", findings=[_make_finding()]
        )
        assert doc.detail == ""

    def test_degraded_empty_findings_blank_detail_now_rejected(self) -> None:
        # Was a regression lock for R2 ("degraded is exempt from the
        # justification requirement entirely, even with empty findings and
        # blank detail"). #1806 explicitly revokes that exemption: a
        # self-reported degraded verdict with no stated reason is now a
        # contract violation, same as "ok" with no findings and no detail.
        with pytest.raises(ValidationError):
            ReviewerFindingsDocument(
                reviewer_role="R", status="degraded", detail="", findings=[]
            )

    def test_failed_status_unaffected_by_justification_check(self) -> None:
        # The new validator doesn't newly constrain "failed" — existing
        # _check_failed_has_no_findings behavior (failed + findings rejected)
        # is covered separately by test_failed_status_with_findings_rejected.
        # detail is non-blank here so the #1806 degraded/failed-reason
        # validator (covered separately below) doesn't fire either.
        doc = ReviewerFindingsDocument(
            reviewer_role="R", status="failed", detail="stated reason", findings=[]
        )
        assert doc.detail == "stated reason"


class TestReviewerFindingsDocumentDegradedFailedJustification:
    """#1806: status='degraded'/'failed' requires a non-blank `detail` stating
    the reason — closes the gap #1775 could not reach (it can only persist a
    reason that exists; it can't require one to exist in the first place).
    """

    def test_degraded_blank_detail_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReviewerFindingsDocument(
                reviewer_role="R", status="degraded", detail="", findings=[]
            )

    def test_degraded_whitespace_detail_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReviewerFindingsDocument(
                reviewer_role="R", status="degraded", detail="   ", findings=[]
            )

    def test_degraded_with_findings_and_blank_detail_still_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReviewerFindingsDocument(
                reviewer_role="R",
                status="degraded",
                detail="",
                findings=[_make_finding()],
            )

    def test_degraded_nonblank_detail_passes(self) -> None:
        doc = ReviewerFindingsDocument(
            reviewer_role="R",
            status="degraded",
            detail="sandbox lacked filesystem access",
            findings=[],
        )
        assert doc.detail == "sandbox lacked filesystem access"

    def test_failed_blank_detail_rejected(self) -> None:
        # findings=[] so _check_failed_has_no_findings doesn't short-circuit
        # first -- chained mode="after" validators stop at the first raise.
        with pytest.raises(ValidationError):
            ReviewerFindingsDocument(
                reviewer_role="R", status="failed", detail="", findings=[]
            )

    def test_failed_nonblank_detail_passes(self) -> None:
        doc = ReviewerFindingsDocument(
            reviewer_role="R", status="failed", detail="crashed on startup", findings=[]
        )
        assert doc.detail == "crashed on startup"

    def test_degraded_null_detail_coerces_to_blank_then_rejected(self) -> None:
        # Proves field-level None->"" coercion runs before the new
        # model-level check sees it.
        with pytest.raises(ValidationError):
            ReviewerFindingsDocument.model_validate(
                {
                    "reviewer_role": "R",
                    "status": "degraded",
                    "detail": None,
                    "findings": [],
                }
            )


class TestReviewerFindingsDocumentNullNormalization:
    """A ``None`` detail/findings (from an OpenAI strict-schema nullable-wrapped
    field, #1364) normalizes to the same default a caller omitting the key
    would get, rather than failing type validation.
    """

    def test_null_detail_normalizes_to_empty_string(self) -> None:
        # Decoupled from status="ok" with empty findings (#1544) and from
        # status="degraded" (#1806, which now requires a stated reason on
        # degraded/failed): the only remaining combination that tolerates a
        # blank/null detail is status="ok" with non-empty findings, so this
        # moves there to keep testing exactly what it intends -- the
        # field-level null->"" coercion, orthogonal to either cross-field
        # justification rule.
        doc = _make_reviewer_doc(_make_finding(), detail=None, status="ok")
        assert doc.detail == ""

    def test_null_findings_normalizes_to_empty_list(self) -> None:
        doc = _make_reviewer_doc(findings=None)
        assert doc.findings == []

    def test_status_failed_with_null_findings_still_passes_no_findings_check(
        self,
    ) -> None:
        # detail uses the conftest _make_reviewer_doc default
        # ("reviewed; no issues found."), non-blank, so this stays clear of
        # the #1806 degraded/failed-reason validator too.
        doc = _make_reviewer_doc(status="failed", findings=None)
        assert doc.findings == []


class TestValidateReviewerDocument:
    def test_invalid_severity_rejected(self) -> None:
        bad = Finding.model_construct(**_finding_kwargs(severity="BOGUS"))
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(bad), _make_diff()
        )
        assert not accepted
        assert rejected[0].reason == "invalid_severity"
        assert rejected[0].raw["severity"] == "BOGUS"

    def test_missing_evidence_rejected(self) -> None:
        bad = Finding.model_construct(**_finding_kwargs(evidence="   "))
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(bad), _make_diff()
        )
        assert not accepted
        assert rejected[0].reason == "missing_evidence"

    def test_evidence_not_in_diff_rejected(self) -> None:
        f = _make_finding(evidence="not present anywhere")
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(f), _make_diff()
        )
        assert not accepted
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_unknown_file_rejected_without_worktree(self) -> None:
        # Exercises the worktree=None back-compat path (#1632): with no
        # worktree opted in, a non-diff file is always "unknown_file",
        # regardless of whether it exists on disk anywhere.
        f = _make_finding(file="src/cw/other.py")
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(f), _make_diff()
        )
        assert not accepted
        assert rejected[0].reason == "unknown_file"

    def test_invalid_line_reference_rejected(self) -> None:
        # #2007 narrowed what this reason covers: a bogus line alone is no
        # longer sufficient, since the evidence may be genuinely present
        # elsewhere in the diff and get content-rescued. The evidence here is
        # fabricated, so the rejection stands.
        f = _make_finding(
            line_start=999, line_end=999, evidence="fabricated absent content"
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(f), _make_diff()
        )
        assert not accepted
        assert rejected[0].reason == "invalid_line_reference"

    def test_sub_must_fix_rejection_logs_info_with_reviewer_severity_reason_title(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # #2000: below MUST_FIX, a mechanical rejection used to leave no trace
        # at all -- not in the log, not on the verdict, not in the comment. The
        # counter is only half the fix; the operator reading the session log
        # must be able to see WHICH finding was deleted and why.
        f = _make_finding(
            severity="SHOULD_FIX",
            evidence="not present anywhere",
            summary="silently dropped finding",
        )
        with caplog.at_level(logging.INFO, logger="cw.review_findings"):
            _accepted, rejected, _stripped = validate_reviewer_document(
                _make_reviewer_doc(f, reviewer_role="Code Quality Reviewer"),
                _make_diff(),
            )
        assert rejected[0].reason == "evidence_not_in_diff"
        assert any(
            "mechanically rejected finding" in rec.getMessage()
            and "Code Quality Reviewer" in rec.getMessage()
            and "SHOULD_FIX" in rec.getMessage()
            and "evidence_not_in_diff" in rec.getMessage()
            and "silently dropped finding" in rec.getMessage()
            for rec in caplog.records
        )

    @pytest.mark.parametrize("severity", ["DEBT", "NIT", "PRINCIPLE"])
    def test_every_sub_must_fix_severity_rejection_logs_info(
        self, severity: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        # #2000: the log line is keyed on the rejection itself, not on a
        # severity floor -- every severity below MUST_FIX is announced too.
        f = _make_finding(severity=severity, file="src/cw/other.py")
        with caplog.at_level(logging.INFO, logger="cw.review_findings"):
            _accepted, rejected, _stripped = validate_reviewer_document(
                _make_reviewer_doc(f), _make_diff()
            )
        assert rejected[0].reason == "unknown_file"
        assert any(
            "mechanically rejected finding" in rec.getMessage()
            and severity in rec.getMessage()
            and "unknown_file" in rec.getMessage()
            for rec in caplog.records
        )

    def test_invalid_line_reference_rejection_logs_its_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # #2000: the third mechanical-rejection reason reaches the same log.
        # Evidence fabricated so #2007's content rescue cannot claim it — the
        # log assertion is about the rejection path, which needs a rejection.
        f = _make_finding(
            severity="NIT",
            line_start=999,
            line_end=999,
            evidence="fabricated absent content",
        )
        with caplog.at_level(logging.INFO, logger="cw.review_findings"):
            _accepted, rejected, _stripped = validate_reviewer_document(
                _make_reviewer_doc(f), _make_diff()
            )
        assert rejected[0].reason == "invalid_line_reference"
        assert any(
            "mechanically rejected finding" in rec.getMessage()
            and "invalid_line_reference" in rec.getMessage()
            for rec in caplog.records
        )

    def test_must_fix_rejection_still_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # #2000 regression: the new log is on the shared append site, so the
        # (already-visible via #1714) MUST_FIX case is announced as well.
        f = _make_finding(severity="MUST_FIX", file="src/cw/other.py")
        with caplog.at_level(logging.INFO, logger="cw.review_findings"):
            _accepted, rejected, _stripped = validate_reviewer_document(
                _make_reviewer_doc(f), _make_diff()
            )
        assert rejected[0].reason == "unknown_file"
        assert any(
            "mechanically rejected finding" in rec.getMessage()
            and "MUST_FIX" in rec.getMessage()
            for rec in caplog.records
        )

    def test_file_level_finding_skips_line_check(self) -> None:
        f = _make_finding(line_start=None, line_end=None)
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(f), _make_diff()
        )
        assert len(accepted) == 1
        assert not rejected

    def test_rejected_preserves_raw_payload(self) -> None:
        # worktree=None explicitly: stays on the no-worktree fallback path
        # (#1632) — the tree-existence relaxation never engages here.
        f = _make_finding(file="src/cw/other.py", summary="raw kept")
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(f), _make_diff(), worktree=None
        )
        assert rejected[0].raw["summary"] == "raw kept"
        assert rejected[0].reviewer_role == "Test Reviewer"

    def test_evidence_from_removed_line_rejected_by_default_claimed_line(self) -> None:
        # R6 (#1236): supersedes A3's file-full-diff matching claim for
        # Finding.evidence. The default finding claims line_start=line_end=10
        # ("def broken():"); evidence quoting a removed/context line elsewhere in
        # the diff is NOT the content of the claimed line, so true line-position
        # validation now rejects it (evidence_not_in_diff), where the old
        # whole-diff substring check accepted it. Escalation quotes are
        # unaffected — see test_quote_matches_full_diff_not_only_added_lines.
        diff = _make_diff(extra_text="-removed_context_line = 1")
        f = _make_finding(evidence="removed_context_line = 1")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert not accepted
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_evidence_from_other_line_same_file_is_now_content_rescued(self) -> None:
        # R6 (#1236) originally pinned this as rejected: a quote that IS the
        # verbatim content of a *different added line in the same file* —
        # outside the finding's claimed line window — used to be rejected
        # (evidence_not_in_diff) even though it's real, in-file content, on
        # the theory that the claimed window (line 10) must itself contain
        # the evidence.
        #
        # #2019 supersedes that: this is exactly the ticket's own motivating
        # shape ("validate on the quote, not just the line number") — the
        # claimed line is an exact anchor hit (10 is a genuine added line),
        # but its content doesn't match; the evidence's true home (line 11)
        # is genuinely, verbatim present elsewhere in the SAME file's diff.
        # _classify_mislocated_finding's unbounded file_window_text search
        # finds it there and accepts, and _resolved_finding's own narrower
        # rescue (over the added-only file_line_text) also finds it, so the
        # persisted anchor is fully corrected to (11, 11) rather than
        # staying at the reviewer's stale (10, 10).
        diff = _make_diff(
            "def broken():",
            "sneaky = elsewhere()",
            files={"src/cw/foo.py": [10, 11]},
        )
        f = _make_finding(evidence="sneaky = elsewhere()", line_start=10, line_end=10)
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert rejected == []
        assert accepted[0].line_start == 11
        assert accepted[0].line_end == 11

    def test_evidence_cross_file_rejected(self) -> None:
        # R6 (#1236): a quote copied verbatim from a *different changed file's*
        # hunk is rejected even though it appears in the full diff text — the
        # claimed file/line window governs, not whole-diff substring presence.
        diff = _make_diff(
            "def broken():",
            "other_file_line = 2",
            files={"src/cw/foo.py": [10], "src/cw/bar.py": [10]},
        )
        # MUST_FIX 3 (#1236): confirm the "stolen" evidence is genuinely
        # present in bar.py's OWN hunk — not just absent everywhere, which
        # would make the rejection below tautological rather than proving
        # file-scoping.
        assert diff.file_line_text["src/cw/bar.py"][10] == "other_file_line = 2"
        assert diff.file_line_text["src/cw/foo.py"][10] == "def broken():"
        f = _make_finding(
            file="src/cw/foo.py",
            evidence="other_file_line = 2",
            line_start=10,
            line_end=10,
        )
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert not accepted
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_single_endpoint_finding_checks_that_line(self) -> None:
        # A finding with only line_start set (line_end None) checks evidence
        # against exactly that one line.
        diff = _make_diff("def broken():", files={"src/cw/foo.py": [10]})
        f = _make_finding(evidence="def broken():", line_start=10, line_end=None)
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected

    def test_single_endpoint_line_end_only_checks_that_line(self) -> None:
        # Symmetric: only line_end set (line_start None).
        diff = _make_diff("def broken():", files={"src/cw/foo.py": [10]})
        f = _make_finding(evidence="def broken():", line_start=None, line_end=10)
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected

    def test_file_level_evidence_matches_file_hunk(self) -> None:
        # A file-level finding (both endpoints None) has no line anchor and
        # falls back to matching against that file's full hunk text
        # (file_diffs), the same fallback _line_reference_valid grants today.
        diff = _make_diff(extra_text="-context_only = 3")
        f = _make_finding(evidence="context_only = 3", line_start=None, line_end=None)
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected

    # -- #1715: near-line anchor tolerance -----------------------------

    def test_near_line_anchor_within_tolerance_retained(self) -> None:
        # Anchor is 2 lines off the real added line (10) — within the
        # +/-3 tolerance bound. Evidence text is correct, so the finding
        # should be retained rather than rejected invalid_line_reference.
        diff = _make_diff("def broken():", files={"src/cw/foo.py": [10]})
        f = _make_finding(line_start=12, line_end=12, evidence="def broken():")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected
        # The accepted finding's anchor is snapped to the real added line
        # (10), not left at the reviewer's raw off-by-2 claim (12) — a
        # downstream renderer showing this location must point at real
        # source, not the reviewer's drift (#1715).
        assert accepted[0].line_start == 10
        assert accepted[0].line_end == 10

    def test_near_line_range_anchor_retained(self) -> None:
        # line_start=8 is 2 lines off added line 10; line_end=13 is 2 lines
        # off added line 11 — each endpoint independently within tolerance.
        diff = _make_diff("line one", "line two", files={"src/cw/foo.py": [10, 11]})
        f = _make_finding(line_start=8, line_end=13, evidence="line one\nline two")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected
        assert accepted[0].line_start == 10
        assert accepted[0].line_end == 11

    # -- #1715: multiline evidence prefix normalization -----------------

    def test_file_level_multiline_evidence_matches_after_prefix_normalization(
        self,
    ) -> None:
        # File-level finding (no line anchor) falls back to file_diffs, which
        # stores raw hunk text with a "+" marker on every line. A reviewer's
        # genuine multiline quote carries no such markers at all (that's the
        # real-world shape of Bug B: a plain source-code quote, not a
        # diff-rendered one) — the *second* line's missing "+" breaks
        # contiguous substring matching against "+line one\n+line two" even
        # though the content is identical. MUST fail red pre-fix (verified:
        # "line one\nline two" is NOT a substring of the raw
        # "+++ b/...\n+line one\n+line two\n" hunk text).
        diff = _make_diff("line one", "line two", files={"src/cw/foo.py": [10, 11]})
        f = _make_finding(line_start=None, line_end=None, evidence="line one\nline two")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected

    def test_windowed_multiline_evidence_with_prefix_still_matches(self) -> None:
        # Windowed finding (explicit line_start/line_end) builds its window
        # from file_line_text, which is already prefix-free. Here the
        # REVIEWER's evidence itself carries diff-style "+" markers (plausible
        # if copied from a rendered diff view) — the latent exposure noted in
        # Bug B's second half. MUST fail red pre-fix: "+line one\n+line two"
        # is not a substring of the prefix-free window "line one\nline two".
        diff = _make_diff("line one", "line two", files={"src/cw/foo.py": [10, 11]})
        f = _make_finding(line_start=10, line_end=11, evidence="+line one\n+line two")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected

    # -- #1715: regression guards (mutation-proof) -----------------------

    def test_anchor_outside_tolerance_fails_the_line_gate(self) -> None:
        # Distance 4 from the only added line (10) — outside the +/-3 bound.
        # Asserted against `_line_reference_valid` directly rather than the
        # document verdict: #2007 added a content rescue downstream of this
        # gate, so a document-level assertion would now pass for the wrong
        # reason (rescued, not near-line-resolved) and stop mutation-proofing
        # the bound. The bound itself is unchanged.
        diff = _make_diff("def broken():", files={"src/cw/foo.py": [10]})
        f = _make_finding(line_start=14, line_end=14, evidence="def broken():")
        assert _line_reference_valid(diff, f) is False

    def test_anchor_outside_tolerance_with_absent_evidence_still_rejected(self) -> None:
        # The same out-of-tolerance anchor, with nothing for #2007's content
        # rescue to find — the pre-#2007 verdict stands.
        diff = _make_diff("def broken():", files={"src/cw/foo.py": [10]})
        f = _make_finding(line_start=14, line_end=14, evidence="fabricated absent text")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert not accepted
        assert rejected[0].reason == "invalid_line_reference"

    def test_anchor_outside_tolerance_with_present_evidence_is_rescued(self) -> None:
        # The #2007 flip side, pinned next to the bound it composes with: the
        # anchor gate still says no, and the content rescue still says yes.
        diff = _make_diff("def broken():", files={"src/cw/foo.py": [10]})
        f = _make_finding(line_start=14, line_end=14, evidence="def broken():")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert rejected == []
        assert accepted[0].line_start == 10

    def test_near_line_content_mismatch_still_rejected(self) -> None:
        # line_start=12 resolves to added line 10 (distance 2, within
        # tolerance), but the evidence text is not that line's real content —
        # the loosened anchor bound must not loosen the evidence check.
        diff = _make_diff("def broken():", files={"src/cw/foo.py": [10]})
        f = _make_finding(line_start=12, line_end=12, evidence="totally unrelated text")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert not accepted
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_widened_range_window_does_not_admit_third_unrelated_line(self) -> None:
        # line_start=8 snaps to 10 (distance 2); line_end=15 snaps to 16
        # (distance 1) -> resolved window is 10-16 inclusive, wider than a
        # single +/-3 span (the deliberate, tested compounding effect from
        # independently-snapped endpoints). Evidence genuinely inside that
        # window (line 13's real content) is accepted.
        diff = _make_diff(
            "first line content",
            "second line content",
            "third line content",
            "fourth line content",
            files={"src/cw/foo.py": [10, 13, 16, 20]},
        )
        f = _make_finding(line_start=8, line_end=15, evidence="second line content")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert len(accepted) == 1
        assert not rejected
        assert accepted[0].line_start == 10
        assert accepted[0].line_end == 16

    def test_widened_range_window_beyond_resolved_bound_is_now_content_rescued(
        self,
    ) -> None:
        # Same widened window (10-16) as above; the evidence is the real
        # content of line 20 — a genuine added line, just outside the
        # resolved window. Pre-#2019 this stayed rejected, proving the
        # widened window was still bounded, not an unbounded escape hatch.
        # #2019 gives the evidence-gate the same unbounded content rescue the
        # anchor-gate already had (#2007) — see
        # TestEvidenceGateContentRescue for the dedicated coverage of that
        # rescue's acceptance/rejection/anchor-correction shape; this test
        # keeps pinning THIS fixture's outcome now that it flips to accepted.
        diff = _make_diff(
            "first line content",
            "second line content",
            "third line content",
            "fourth line content",
            files={"src/cw/foo.py": [10, 13, 16, 20]},
        )
        f = _make_finding(line_start=8, line_end=15, evidence="fourth line content")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert not rejected
        assert accepted[0].line_start == 20
        assert accepted[0].line_end == 20

    # -- #1738: hunk-context evidence window (mode 4) --------------------

    def test_hunk_context_window_evidence_retained(self) -> None:
        # Real #1729 diagnostics artifact (see module fixtures above): a
        # SHOULD_FIX finding whose evidence is a verbatim 6-line quote of the
        # post-change source (lines 9522-9527), but only line 9526 of that
        # span is a diff-added line -- the other five are unchanged context
        # that _resolve_line_window's added-line-only window used to drop,
        # rejecting a genuine, fully-verbatim quote as evidence_not_in_diff.
        # MUST fail red pre-fix: CapturedDiff has no file_window_text field
        # yet, and _evidence_in_claimed_lines still routes through
        # _resolve_line_window/file_line_text (added-only), which snaps this
        # claim's window down to (9521, 9526) and drops the tail of the quote.
        diff = _pr1729_captured_diff()
        finding = Finding(**_PR1729_REJECTED_FINDING_KWARGS)
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert not rejected
        assert len(accepted) == 1
        # The persisted anchor stays snapped to the genuine added line (9526),
        # not the reviewer's raw claimed endpoints (9522/9527) -- both of
        # which are context lines. _resolved_finding deliberately keeps
        # calling the unchanged _resolve_line_window (added-only), not the
        # new _resolve_hunk_window.
        assert accepted[0].line_start == 9521
        assert accepted[0].line_end == 9526

    def test_hunk_context_window_unrelated_line_now_content_rescued(self) -> None:
        # Negative control: widening the window to include context-line
        # content must not turn it into "match anything nearby". Real diff,
        # real content -- but neither variant's evidence is the true content
        # of the claimed 9522-9527 window.
        diff = _pr1729_captured_diff()

        # (a) genuine CONTEXT-line content from elsewhere in the same file
        # (line 9511, "from cw.auto_dev_result import ("), claimed at the
        # #1729 finding's line range. Only visible in file_window_text at
        # all post-#1738 fix -- proved the widened evidence-quote check
        # (_evidence_in_claimed_lines) was still bounded to the claimed
        # window, not a whole-file search. #2019 adds a SEPARATE unbounded
        # rescue one gate later (_classify_mislocated_finding, reusing
        # #2007's _content_rescue_anchor): once the evidence-gate misses,
        # that rescue does search the whole file_window_text, finds this
        # evidence's genuine home at line 9511, and accepts the finding.
        #
        # The PERSISTED anchor is NOT snapped to 9511, though: line 9511 is
        # a context line, and _resolved_finding's own rescue only searches
        # the narrower added-lines-only file_line_text (#1738's invariant --
        # a persisted anchor never points at a context line), where 9511
        # doesn't appear at all. So the anchor stays at whatever
        # _resolve_line_window resolved the reviewer's original 9522-9527
        # claim to over that added-only substrate -- 9521-9526, the exact
        # same persisted span test_hunk_context_window_evidence_retained
        # pins for the sibling #1729 finding above (evidence-independent:
        # resolution runs before the evidence-quote check).
        unrelated_context = _make_finding(
            file="tests/test_dispatch.py",
            line_start=9522,
            line_end=9527,
            evidence="from cw.auto_dev_result import (",
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(unrelated_context), diff
        )
        assert not rejected
        assert accepted[0].line_start == 9521
        assert accepted[0].line_end == 9526

        # (b) evidence that only ever existed on a REMOVED line -- no
        # new-file line number at all, so it can never be in file_window_text
        # regardless of window width. line_start/line_end=9494 is itself a
        # genuine added line (exact hit, no snapping), so this fails on the
        # evidence check, not invalid_line_reference.
        removed_line_evidence = _make_finding(
            file="tests/test_dispatch.py",
            line_start=9494,
            line_end=9494,
            evidence="OPERATOR_UNAVAILABLE_BLOCKER_REASONS.",
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(removed_line_evidence), diff
        )
        assert not accepted
        assert rejected[0].reason == "evidence_not_in_diff"


class Test9491MustFixCaseReconstruction:
    """#1764: reconstructs the genuine tests/test_dispatch.py:9491 MUST_FIX
    (reported via GitHub comment id 5226090232 — see
    ``_PR1729_9491_MUST_FIX_FINDING_KWARGS`` above for the provenance
    disclosure) and proves the whole-function structural-claim rejection
    mode it exhibits is still active against the current matcher: the anchor
    resolves fine (both #1715's near-line tolerance and #1743's enclosing-def
    fallback are proven working elsewhere in this file), but the evidence is
    reviewer prose describing an aggregate property of the function rather
    than a verbatim quote of diff-resident text, so it is rejected
    ``evidence_not_in_diff`` — a third axis, distinct from both #1743
    (anchor validity) and #1738 (window construction).

    #1816 root-caused which check actually fails and concluded the rejection
    is CORRECT — no predicate change, matcher untouched:

    - The failing predicate is ``_evidence_in_claimed_lines``
      (``src/cw/review_findings.py:821``), specifically its
      ``_reconcile_evidence_window`` call
      (``src/cw/review_findings.py:863``), invoked from
      ``_classify_finding`` (``src/cw/review_findings.py:1018-1021``).
      ``_line_reference_valid`` (``src/cw/review_findings.py:650``) PASSES —
      the claimed line 9491 resolves via ordinary #1715 near-line tolerance
      (nearest added line 9494, distance 3) — and stays completely untouched
      by #1816; it was never the failing check for this fixture.
    - The finding's evidence
      ("...now exceeds the 50-line function threshold and covers two
      independent contracts.") is not a quote of any diff line — it
      describes an aggregate property of the whole function body. No amount
      of window widening can make a structural claim like this satisfy a
      verbatim-substring check, because the property being asserted has no
      diff-resident string form at any offset.
    - Verdict is corroborated by three independent sources: (1) the reviewer
      contract itself mandates verbatim evidence —
      ``.claude/commands/auto-dev-review.md:121`` (``"evidence": "<verbatim
      diff substring>"``) and ``:129`` ("`evidence` MUST be a verbatim
      substring of the diff text at the claimed lines — `cw review
      consolidate` (Checkpoint 3a) rejects any finding whose evidence
      doesn't literally appear there."); (2) ``_classify_finding``'s own
      docstring already anticipates and names this exact outcome ("This can
      turn a previously `invalid_line_reference` case into
      `evidence_not_in_diff` at the very next check below — intentional;
      #1743 owns the anchor-resolution axis, #1738 owns evidence-quote
      matching."); (3) the sibling fixture
      ``TestPromptsGetPurposePromptStructuralFinding`` below reproduces the
      identical shape on an independent real diff (#1703/
      ``get_purpose_prompt``), and its own test —
      ``test_1703_classified_evidence_not_in_diff_matching_production`` — is
      already named to assert this is expected, real production behavior,
      not a bug.
    - Conclusion: the class of finding this fixture reconstructs (a
      whole-function structural claim with no verbatim diff quote) is a
      defect in the reviewer/codex output contract, not in this matcher —
      out of scope for a matcher fix. See #1816 for the full investigation.
    """

    def test_9491_line_reference_valid_via_near_line_tolerance(self) -> None:
        # Resolves via ordinary #1715 near-line tolerance (claimed 9491 is
        # distance 3 from the nearest genuine added line, 9494) -- NOT
        # #1743's enclosing-def fallback (no worktree is even passed here).
        diff = _pr1729_captured_diff()
        finding = Finding(**_PR1729_9491_MUST_FIX_FINDING_KWARGS)
        assert _line_reference_valid(diff, finding) is True

    def test_9491_evidence_check_is_the_specific_failing_predicate(self) -> None:
        # #1816: locks in WHICH check fails. _line_reference_valid passes
        # (anchor resolves via ordinary #1715 near-line tolerance -- claimed
        # 9491 is distance 3 from the nearest genuine added line, 9494) --
        # only _evidence_in_claimed_lines fails, because finding.evidence is
        # aggregate prose about the whole function, never a verbatim diff
        # quote.
        diff = _pr1729_captured_diff()
        finding = Finding(**_PR1729_9491_MUST_FIX_FINDING_KWARGS)
        assert _line_reference_valid(diff, finding) is True
        assert (
            _evidence_in_claimed_lines(
                diff,
                finding.file,
                finding.evidence,
                finding.line_start,
                finding.line_end,
            )
            is False
        )

    def test_9491_classified_evidence_not_in_diff(self) -> None:
        diff = _pr1729_captured_diff()
        finding = Finding(**_PR1729_9491_MUST_FIX_FINDING_KWARGS)
        changed = frozenset(diff.files)
        assert _classify_finding(finding, diff, changed) == "evidence_not_in_diff"

    def test_9491_rejected_via_validate_reviewer_document(self) -> None:
        diff = _pr1729_captured_diff()
        finding = Finding(**_PR1729_9491_MUST_FIX_FINDING_KWARGS)
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert accepted == []
        assert rejected[0].reason == "evidence_not_in_diff"
        assert rejected[0].detail == (
            "evidence is 1 line(s) long but the declared range "
            "line_start=9491, line_end=None spans 1 line(s); no window "
            "within ±3 lines of the declared range contains the "
            "evidence text verbatim"
            + _UNBOUNDED_RESCUE_MISS_DIAGNOSIS
            + _RESCUE_NOT_ATTEMPTED_DIAGNOSIS
        )

    def test_9491_parks_as_rejected_must_fix_via_consolidate_verdict(self) -> None:
        # Mirrors
        # test_mechanically_rejected_must_fix_populates_rejected_must_fix_field's
        # shape: blocking stays False (R4 -- an unreliable/unadjudicated
        # MUST_FIX must never enter the autofix loop), but rejected_must_fix
        # is the independent signal that surfaces it to the operator.
        diff = _pr1729_captured_diff()
        finding = Finding(**_PR1729_9491_MUST_FIX_FINDING_KWARGS)
        doc = _make_reviewer_doc(finding)
        verdict = consolidate_verdict([doc], diff, reviewed_sha="b5c8119e")
        assert verdict.blocking is False
        assert verdict.must_fix == []
        assert len(verdict.rejected_must_fix) == 1
        assert verdict.rejected_must_fix[0].reason == "evidence_not_in_diff"
        assert verdict.rejected_must_fix[0].raw["severity"] == "MUST_FIX"


class TestEvidenceWindowReconciliation:
    """#1792: a MUST_FIX finding whose evidence is diff-resident but whose
    declared line_start/line_end undershoots (or, symmetrically, starts too
    late) the evidence's true span by a small, within-tolerance amount is
    reconciled by :func:`_reconcile_evidence_window`, not mechanically
    rejected ``evidence_not_in_diff``. Real #1784 diff fixture (see module
    fixtures above): an 11-line evidence quote at scripts/install-skills.sh
    new-file lines 235-245, where lines 235-243 are genuinely diff-added and
    244-245 are unchanged hunk-context -- the exact dual-substrate shape
    #1792 exists to reconcile.
    """

    def test_1784_regression_line_end_short_by_one_is_accepted(self) -> None:
        # Declared line_end=244 is one short of the evidence's true end
        # (245). MUST fail red pre-fix: pre-#1792, _evidence_in_claimed_lines
        # builds its window from _resolve_hunk_window(235, 244) = (235, 244)
        # unchanged -- 10 lines of window content -- which cannot contain
        # the 11-line evidence as a substring.
        diff = _pr1784_captured_diff()
        finding = _make_finding(
            file="scripts/install-skills.sh",
            line_start=235,
            line_end=244,
            evidence=_PR1784_EVIDENCE_11_LINES,
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert len(accepted) == 1
        assert rejected == []

    def test_1784_regression_persisted_anchor_is_repaired_to_true_end(self) -> None:
        # Same fixture. The persisted anchor is corrected from the
        # reviewer's stale line_end=244 -- but stays bounded at line 243,
        # the last genuine ADDED line, never extending onto line 245's real
        # content, which is unchanged hunk-context. _resolved_finding
        # deliberately reconciles only against file_line_text (added-only),
        # preserving the same #1738 invariant
        # test_hunk_context_window_evidence_retained pins for the sibling
        # #1729 fixture (persisted anchor 9521-9526, not extending to the
        # reviewer's claimed 9527): a persisted anchor must always point at
        # a real added line, never a context line, even though the
        # (separately, more permissively matched) evidence-quote check spans
        # further via file_window_text.
        diff = _pr1784_captured_diff()
        finding = _make_finding(
            file="scripts/install-skills.sh",
            line_start=235,
            line_end=244,
            evidence=_PR1784_EVIDENCE_11_LINES,
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert not rejected
        assert accepted[0].line_start == 235
        assert accepted[0].line_end == 243

    def test_line_end_short_by_two_within_tolerance_is_accepted(self) -> None:
        # Short by 2 lines (declared line_end=243, true end 245) -- proves
        # the fix isn't a one-off special case for exactly 1 line short.
        diff = _pr1784_captured_diff()
        finding = _make_finding(
            file="scripts/install-skills.sh",
            line_start=235,
            line_end=243,
            evidence=_PR1784_EVIDENCE_11_LINES,
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert len(accepted) == 1
        assert rejected == []

    def test_line_start_late_is_also_reconciled(self) -> None:
        # Symmetric case: line_start is declared 2 lines AFTER the
        # evidence's true start (235) -- the evidence's leading two lines
        # are dropped by the declared window. line_end=245 is itself a
        # genuine hunk-context line (exact hit), so only the start side
        # needs widening -- proves reconciliation isn't end-only.
        diff = _pr1784_captured_diff()
        finding = _make_finding(
            file="scripts/install-skills.sh",
            line_start=237,
            line_end=245,
            evidence=_PR1784_EVIDENCE_11_LINES,
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert len(accepted) == 1
        assert rejected == []

    def test_line_end_short_beyond_tolerance_now_content_rescued(self) -> None:
        # Short by _LINE_ANCHOR_TOLERANCE + 1 (4 lines: declared line_end=
        # 241, true end 245) -- outside _reconcile_evidence_window's bound at
        # the evidence-gate, which pre-#2019 was terminal (evidence_not_in_
        # diff). #2019's _classify_mislocated_finding rescue then searches
        # file_window_text unbounded, finds the evidence's true 235-245 span
        # (added lines 235-243 plus context lines 244-245), and accepts.
        #
        # The PERSISTED anchor, however, stays at (235, 241) -- unchanged
        # from the reviewer's declared range. _resolved_finding's own rescue
        # (added the same #2019 shape as _classify_mislocated_finding's, but
        # scoped to the narrower file_line_text substrate) tries to repair it
        # but can't: file_line_text has no entries for 244/245 (unchanged
        # hunk-context, never in the added-only map), so no window in that
        # narrower substrate can ever join into the full 11-line evidence.
        # This mirrors the existing #1738 invariant (see
        # test_hunk_context_window_evidence_retained and
        # test_1784_regression_persisted_anchor_is_repaired_to_true_end): a
        # persisted anchor must always point at a real added line, never a
        # context line, even when the (separately, more permissively
        # matched) evidence-quote check spans further via file_window_text.
        diff = _pr1784_captured_diff()
        finding = _make_finding(
            file="scripts/install-skills.sh",
            line_start=235,
            line_end=241,
            evidence=_PR1784_EVIDENCE_11_LINES,
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert rejected == []
        assert len(accepted) == 1
        assert accepted[0].line_start == 235
        assert accepted[0].line_end == 241

    def test_evidence_genuinely_absent_still_rejected(self) -> None:
        # AC2 (negative control): same declared window as the regression
        # case, but the evidence text is not present anywhere in the file's
        # diff at any offset -- the reconciliation must not turn into
        # "accept anything nearby".
        diff = _pr1784_captured_diff()
        finding = _make_finding(
            file="scripts/install-skills.sh",
            line_start=235,
            line_end=244,
            evidence=_PR1784_ABSENT_EVIDENCE,
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert not accepted
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_rejected_finding_detail_reports_discrepancy(self) -> None:
        # AC4: the genuinely-absent-evidence rejection populates `detail`
        # with both the evidence's own line count (11) and the declared
        # line_start (235).
        diff = _pr1784_captured_diff()
        finding = _make_finding(
            file="scripts/install-skills.sh",
            line_start=235,
            line_end=244,
            evidence=_PR1784_ABSENT_EVIDENCE,
        )
        _, rejected, _ = validate_reviewer_document(_make_reviewer_doc(finding), diff)
        assert rejected[0].reason == "evidence_not_in_diff"
        assert "line_start=235" in rejected[0].detail
        assert "11" in rejected[0].detail

    def test_non_evidence_not_in_diff_rejection_keeps_detail_blank(self) -> None:
        # A rejection for any other reason (here unknown_file) keeps detail
        # at its "" default -- detail is populated for evidence_not_in_diff
        # specifically, not for every rejection reason.
        finding = _make_finding(file="src/cw/other.py")
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), _make_diff()
        )
        assert rejected[0].reason == "unknown_file"
        assert rejected[0].detail == ""

    def test_consolidate_verdict_1784_case_no_longer_blocks_via_rejected_must_fix(
        self,
    ) -> None:
        # Integration, mirrors test_mechanically_rejected_must_fix_populates_
        # rejected_must_fix_field: the #1784 fixture at severity="MUST_FIX"
        # (the _make_finding default) run through consolidate_verdict.
        # Post-fix: blocks via the ordinary must_fix path, not the #1714
        # mechanical-rejection park.
        diff = _pr1784_captured_diff()
        finding = _make_finding(
            severity="MUST_FIX",
            file="scripts/install-skills.sh",
            line_start=235,
            line_end=244,
            evidence=_PR1784_EVIDENCE_11_LINES,
        )
        doc = _make_reviewer_doc(finding)
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        assert verdict.blocking is True
        assert len(verdict.must_fix) == 1
        assert verdict.rejected_must_fix == []

    def test_consolidate_verdict_unmatched_must_fix_still_blocks_via_park_signal(
        self,
    ) -> None:
        # AC5, #1714 preservation (negative case): a MUST_FIX whose evidence
        # is genuinely absent must still route through the #1714
        # mechanical-rejection park -- #1792's reconciliation must never
        # widen far enough to rescue a fabricated quote.
        diff = _pr1784_captured_diff()
        finding = _make_finding(
            severity="MUST_FIX",
            file="scripts/install-skills.sh",
            line_start=235,
            line_end=244,
            evidence=_PR1784_ABSENT_EVIDENCE,
        )
        doc = _make_reviewer_doc(finding)
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        assert verdict.blocking is False
        assert len(verdict.rejected_must_fix) == 1
        assert verdict.rejected_must_fix[0].reason == "evidence_not_in_diff"

    def test_persisted_anchor_repaired_when_undershoot_stays_within_added_lines(
        self,
    ) -> None:
        # Sibling of the #1784 fixture's persisted-anchor test, but for the
        # case that fixture deliberately can't exercise: here the evidence's
        # undershot tail (line 12) is itself a genuine ADDED line (not
        # hunk-context), so _resolved_finding's file_line_text-only
        # reconciliation (unlike the #1784 case, where the tail is
        # context-only) DOES find a match and repairs the persisted anchor.
        diff = _make_diff(
            "line one",
            "line two",
            "line three",
            files={"src/cw/foo.py": [10, 11, 12]},
        )
        f = _make_finding(
            line_start=10, line_end=11, evidence="line one\nline two\nline three"
        )
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert not rejected
        assert len(accepted) == 1
        assert accepted[0].line_start == 10
        assert accepted[0].line_end == 12

    def test_reconcile_evidence_window_direct_start_after_end_no_match(self) -> None:
        # Direct unit test of the defensive candidate_start > candidate_end
        # guard: unreachable through validate_reviewer_document (every
        # caller passes an already-ordered start <= end), but exercised
        # directly here so the guard itself is covered rather than dead.
        assert _reconcile_evidence_window({}, "x", start=5, end=3, tolerance=3) is None

    def test_file_level_rejection_detail_reports_no_line_anchor(self) -> None:
        # AC4, file-level branch: a rejected file-level finding (no line
        # anchor at all) gets a detail message naming the no-anchor case
        # rather than a declared-range mismatch.
        f = _make_finding(
            line_start=None, line_end=None, evidence="not present anywhere"
        )
        _, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), _make_diff())
        assert rejected[0].reason == "evidence_not_in_diff"
        assert "no line" in rejected[0].detail
        assert "file-level fallback" in rejected[0].detail


class TestEvidenceGateContentRescue:
    """#2019: give the evidence-gate (``_evidence_in_claimed_lines``) the same
    unbounded content rescue the anchor-gate already has via #2007's
    ``_content_rescue_anchor``. A finding whose anchor resolves fine but whose
    evidence doesn't fit the (already #1792-widened) window is no longer
    mechanically discarded when the evidence text is genuinely present
    elsewhere in the file's diff — a stale line number is weaker evidence than
    the reviewer's own verbatim quote.
    """

    def test_evidence_beyond_widened_window_is_rescued_and_anchor_corrected(
        self,
    ) -> None:
        # Same fixture as test_widened_range_window_rejects_evidence_outside_
        # resolved_window (10-16 resolved window; evidence is line 20's real
        # content) -- pre-#2019 that stayed rejected. Now the evidence-gate
        # miss falls through to the unbounded content rescue, which finds it
        # at line 20, and the persisted anchor is corrected to point there.
        diff = _make_diff(
            "first line content",
            "second line content",
            "third line content",
            "fourth line content",
            files={"src/cw/foo.py": [10, 13, 16, 20]},
        )
        f = _make_finding(line_start=8, line_end=15, evidence="fourth line content")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert not rejected
        assert accepted[0].line_start == 20
        assert accepted[0].line_end == 20

    def test_evidence_beyond_widened_window_rescued_at_should_fix_too(
        self,
    ) -> None:
        # Comment 1's widening: the rescue applies uniformly regardless of
        # severity -- _classify_anchored_finding/_evidence_in_claimed_lines
        # carry no severity branch at all, so this is the same shape at
        # SHOULD_FIX rather than MUST_FIX.
        diff = _make_diff(
            "first line content",
            "second line content",
            "third line content",
            "fourth line content",
            files={"src/cw/foo.py": [10, 13, 16, 20]},
        )
        f = _make_finding(
            severity="SHOULD_FIX",
            line_start=8,
            line_end=15,
            evidence="fourth line content",
        )
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert not rejected
        assert accepted[0].line_start == 20
        assert accepted[0].line_end == 20

    def test_fabricated_evidence_beyond_window_still_rejected(self) -> None:
        # Negative control: #1714's false-accept floor is inherited unchanged
        # -- fabricated evidence found nowhere in the file stays rejected,
        # mirroring test_anchor_outside_tolerance_with_absent_evidence_still_
        # rejected's role for the sibling anchor-gate rescue.
        diff = _make_diff(
            "first line content",
            "second line content",
            "third line content",
            "fourth line content",
            files={"src/cw/foo.py": [10, 13, 16, 20]},
        )
        f = _make_finding(line_start=8, line_end=15, evidence="never appears anywhere")
        accepted, rejected, _ = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert not accepted
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_sibling_reviewer_clean_anchor_absorbs_drifted_twin(self) -> None:
        # #2019's suggested fix #5 (the #21 sibling-reviewer-dedup case),
        # emergent from the anchor-correction above with no new dedup code:
        # once the drifted twin's persisted anchor is corrected to match the
        # clean copy's, dedupe_findings' existing
        # (severity, file, line_start, line_end, evidence) key collapses them.
        diff = _make_diff(
            "first line content",
            "second line content",
            "third line content",
            "fourth line content",
            files={"src/cw/foo.py": [10, 13, 16, 20]},
        )
        drifted = _make_finding(
            line_start=8, line_end=15, evidence="fourth line content"
        )
        clean = _make_finding(
            line_start=20, line_end=20, evidence="fourth line content"
        )
        doc_a = _make_reviewer_doc(drifted, reviewer_role="Reviewer A")
        doc_b = _make_reviewer_doc(clean, reviewer_role="Reviewer B")
        accepted_a, rejected_a, _ = validate_reviewer_document(doc_a, diff)
        accepted_b, rejected_b, _ = validate_reviewer_document(doc_b, diff)
        assert not rejected_a
        assert not rejected_b
        merged = dedupe_findings(
            [("Reviewer A", accepted_a[0]), ("Reviewer B", accepted_b[0])]
        )
        assert len(merged) == 1
        assert merged[0].reviewers == ["Reviewer A", "Reviewer B"]


class TestFormattingTolerantEvidenceMatching:
    """#1976: mechanical evidence-verification must not reject a genuine
    finding over formatting trivia — a Unicode em dash where the diff carries
    ASCII ``--``, or a ``-``/``+`` diff-pair quote for a 1-line declared range
    (whose removed half lives only in ``file_diffs``). Genuinely fabricated
    evidence stays rejected under every normalization and the rescue: this is
    a strictly additive relaxation, never a broadening of what counts as "in
    the diff" (#1714's false-accept floor).
    """

    # -- Phase 1 items 1-4: direct unit tests of the new helpers ---------

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("+added line", "added line"),
            ("-removed line", "removed line"),
            ("  no marker  ", "no marker"),
            ("+a\n-b\nc", "a\nb\nc"),
            ("", ""),
        ],
    )
    def test_strip_diff_markers(self, raw: str, expected: str) -> None:
        assert _strip_diff_markers(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (f"rewritten {_EM_DASH} see it", "rewritten -- see it"),
            (f"range 1{_EN_DASH}2", "range 1-2"),
            (f"it{_RSQUO}s {_LSQUO}q{_RSQUO}", "it's 'q'"),
            (f"{_LDQUO}quoted{_RDQUO}", '"quoted"'),
            (f"a{_NBSP}b", "a b"),
            ("plain ascii -- 'x' \"y\"", "plain ascii -- 'x' \"y\""),
            (
                f"{_LDQUO}a{_RDQUO} {_EM_DASH} b {_EN_DASH} c{_RSQUO}d",
                '"a" -- b - c\'d',
            ),
        ],
    )
    def test_normalize_unicode_punctuation(self, raw: str, expected: str) -> None:
        assert _normalize_unicode_punctuation(raw) == expected

    def test_normalize_diff_text_composes_both_stages(self) -> None:
        # One input carrying BOTH a diff marker and unicode punctuation: the
        # marker strip runs first, then the punctuation fold.
        raw = f"+    prefix = {_LDQUO}x{_RDQUO} {_EM_DASH} note"
        assert _normalize_diff_text(raw) == 'prefix = "x" -- note'

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("-x\n+y", ("-x", "+y")),
            ("-x", None),
            ("-x\n+y\n+z", None),
            ("+x\n-y", None),
            ("-x\n-y", None),
        ],
    )
    def test_evidence_diff_pair(
        self, raw: str, expected: tuple[str, str] | None
    ) -> None:
        assert _evidence_diff_pair(raw) == expected

    # -- Phase 1 items 5-6: the `-`/`+` diff-pair rescue ------------------

    def test_diff_pair_evidence_for_single_line_range_is_accepted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Round-1 shape. MUST fail red pre-fix: the declared 1-line window's
        # content is the ADDED line only, so the 2-line pair can be neither a
        # substring of it (phase 1) nor exactly equal to any widened window
        # (phase 2) -- the removed half exists only in file_diffs.
        diff = _issue1879_captured_diff()
        finding = _make_finding(
            file=_ISSUE1879_FILE,
            line_start=_ISSUE1879_ADDED_LINE_NO,
            line_end=_ISSUE1879_ADDED_LINE_NO,
            evidence=_ISSUE1879_DIFF_PAIR_EVIDENCE,
        )
        with caplog.at_level(logging.INFO):
            accepted, rejected, _ = validate_reviewer_document(
                _make_reviewer_doc(finding), diff
            )
        assert len(accepted) == 1
        assert rejected == []
        assert any("diff-pair evidence match" in r.getMessage() for r in caplog.records)

    def test_diff_pair_rescue_matches_on_removed_half_alone(self) -> None:
        # Symmetric case: only the REMOVED half is real -- the substrate the
        # windowed check structurally cannot reach. Proves the rescue's
        # removed-line branch carries the match on its own.
        diff = _issue1879_captured_diff()
        finding = _make_finding(
            file=_ISSUE1879_FILE,
            line_start=_ISSUE1879_ADDED_LINE_NO,
            line_end=_ISSUE1879_ADDED_LINE_NO,
            evidence=f'-{_ISSUE1879_REMOVED_LINE}\n+    prefix = "never written"',
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert len(accepted) == 1
        assert rejected == []

    # -- Phase 1 items 7-9: unicode-punctuation tolerance -----------------

    def test_em_dash_evidence_against_ascii_double_hyphen_is_accepted(self) -> None:
        # Round-2 shape, the ticket's headline case. MUST fail red pre-fix:
        # `—` is not `--` under a raw substring comparison.
        diff = _issue1879_captured_diff()
        finding = _make_finding(
            file=_ISSUE1879_FILE,
            line_start=_ISSUE1879_ADDED_LINE_NO,
            line_end=_ISSUE1879_ADDED_LINE_NO,
            evidence=(f'    prefix = "modern"  # rewritten {_EM_DASH} see the ticket'),
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert len(accepted) == 1
        assert rejected == []

    def test_evidence_missing_leading_plus_marker_still_accepted(self) -> None:
        # Regression lock on behavior that already passes pre-#1976 via
        # #1715's per-line marker strip -- the split into
        # _strip_diff_markers/_normalize_unicode_punctuation must not
        # regress it.
        diff = _issue1879_captured_diff()
        finding = _make_finding(
            file=_ISSUE1879_FILE,
            line_start=_ISSUE1879_ADDED_LINE_NO,
            line_end=_ISSUE1879_ADDED_LINE_NO,
            evidence=_ISSUE1879_ADDED_LINE,
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert len(accepted) == 1
        assert rejected == []

    def test_curly_quote_evidence_against_straight_quotes_is_accepted(self) -> None:
        diff = _issue1879_captured_diff()
        finding = _make_finding(
            file=_ISSUE1879_FILE,
            line_start=_ISSUE1879_ADDED_LINE_NO,
            line_end=_ISSUE1879_ADDED_LINE_NO,
            evidence=(
                f"    prefix = {_LDQUO}modern{_RDQUO}  # rewritten -- see the ticket"
            ),
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert len(accepted) == 1
        assert rejected == []

    def test_nbsp_evidence_against_ascii_space_is_accepted(self) -> None:
        diff = _issue1879_captured_diff()
        finding = _make_finding(
            file=_ISSUE1879_FILE,
            line_start=_ISSUE1879_ADDED_LINE_NO,
            line_end=_ISSUE1879_ADDED_LINE_NO,
            evidence=(f'    prefix = "modern"  #{_NBSP}rewritten -- see the ticket'),
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert len(accepted) == 1
        assert rejected == []

    # -- Phase 1 items 10-12: the relaxation stays bounded ----------------

    def test_pair_evidence_against_multi_line_range_stays_rejected(self) -> None:
        # The rescue is scoped to a 1-line declared range: a pair-shaped
        # quote declared against a 3-line range gets no rescue, so the
        # ordinary windowed rejection stands.
        diff = _issue1879_captured_diff()
        finding = _make_finding(
            file=_ISSUE1879_FILE,
            line_start=_ISSUE1879_ADDED_LINE_NO,
            line_end=_ISSUE1879_ADDED_LINE_NO + 2,
            evidence=_ISSUE1879_DIFF_PAIR_EVIDENCE,
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert accepted == []
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_fabricated_pair_evidence_still_rejected(self) -> None:
        # #1714 floor: pair-shaped and correctly-anchored, but neither half
        # is anywhere in the file's raw diff text.
        diff = _issue1879_captured_diff()
        finding = _make_finding(
            file=_ISSUE1879_FILE,
            line_start=_ISSUE1879_ADDED_LINE_NO,
            line_end=_ISSUE1879_ADDED_LINE_NO,
            evidence="-    ghost_removed = 1\n+    ghost_added = 2",
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert accepted == []
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_fabricated_non_pair_evidence_still_rejected(self) -> None:
        diff = _issue1879_captured_diff()
        finding = _make_finding(
            file=_ISSUE1879_FILE,
            line_start=_ISSUE1879_ADDED_LINE_NO,
            line_end=_ISSUE1879_ADDED_LINE_NO,
            evidence="    this line was never written by anyone",
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert accepted == []
        assert rejected[0].reason == "evidence_not_in_diff"

    # -- Phase 1 item 13: per-stage rejection diagnostics -----------------

    def test_detail_reports_rescue_not_attempted_for_non_pair_evidence(self) -> None:
        diff = _issue1879_captured_diff()
        finding = _make_finding(
            file=_ISSUE1879_FILE,
            line_start=_ISSUE1879_ADDED_LINE_NO,
            line_end=_ISSUE1879_ADDED_LINE_NO,
            evidence="    this line was never written by anyone",
        )
        _, rejected, _ = validate_reviewer_document(_make_reviewer_doc(finding), diff)
        assert rejected[0].detail.endswith(_RESCUE_NOT_ATTEMPTED_DIAGNOSIS)

    def test_detail_reports_rescue_not_attempted_for_multi_line_range(self) -> None:
        diff = _issue1879_captured_diff()
        finding = _make_finding(
            file=_ISSUE1879_FILE,
            line_start=_ISSUE1879_ADDED_LINE_NO,
            line_end=_ISSUE1879_ADDED_LINE_NO + 2,
            evidence="-    ghost_removed = 1\n+    ghost_added = 2",
        )
        _, rejected, _ = validate_reviewer_document(_make_reviewer_doc(finding), diff)
        assert rejected[0].detail.endswith(_RESCUE_NOT_ATTEMPTED_DIAGNOSIS)

    def test_detail_reports_rescue_attempted_for_single_line_pair(self) -> None:
        diff = _issue1879_captured_diff()
        finding = _make_finding(
            file=_ISSUE1879_FILE,
            line_start=_ISSUE1879_ADDED_LINE_NO,
            line_end=_ISSUE1879_ADDED_LINE_NO,
            evidence="-    ghost_removed = 1\n+    ghost_added = 2",
        )
        _, rejected, _ = validate_reviewer_document(_make_reviewer_doc(finding), diff)
        assert rejected[0].detail.endswith(_RESCUE_ATTEMPTED_DIAGNOSIS)

    def test_detail_reports_rescue_not_applicable_for_file_level_finding(self) -> None:
        # File-level fallback: no line anchor at all, so neither the declared
        # range nor the pair shape has anything to resolve against.
        diff = _issue1879_captured_diff()
        finding = _make_finding(
            file=_ISSUE1879_FILE,
            line_start=None,
            line_end=None,
            evidence="    this line was never written by anyone",
        )
        _, rejected, _ = validate_reviewer_document(_make_reviewer_doc(finding), diff)
        assert rejected[0].detail.endswith(_RESCUE_NOT_APPLICABLE_DIAGNOSIS)

    # -- Phase 1 items 15-16: the escalation-quote path -------------------

    def test_escalation_quote_with_em_dash_survives_normalization(self) -> None:
        # Mirrors the primary path's em-dash case on the separate
        # _substring_in_diff predicate. Pre-fix the escalation was stripped:
        # `—` is not `--` under the raw `text in diff.text` check.
        diff = _make_diff("def broken():", "note -- rewritten")
        esc = _make_escalation(evidence_quote=f"note {_EM_DASH} rewritten")
        finding = _make_finding(escalation=esc)
        accepted, _, stripped = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert not stripped
        assert accepted[0].escalation is not None

    def test_escalation_quote_fabricated_still_stripped(self) -> None:
        # The wider normalization is strictly additive on this path too: a
        # quote absent under BOTH the marker strip and the unicode fold is
        # still stripped.
        diff = _make_diff("def broken():", "note -- rewritten")
        esc = _make_escalation(
            evidence_quote=f"ghost quote {_EM_DASH} genuinely absent"
        )
        finding = _make_finding(escalation=esc)
        accepted, _, stripped = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert accepted[0].escalation is None
        assert len(stripped) == 1
        assert stripped[0].reason == "escalation_evidence_not_in_diff"


class TestUnanchoredFindings:
    """#1632: a finding whose file is not in the diff but does resolve to a
    real path under an opted-in ``worktree`` is routed to adjudication
    (``"unanchored"``) instead of being silently discarded as
    ``"unknown_file"``. Tree-existence proves the *path* is real, never the
    evidence *quote* — the escalation-quote check still runs against the
    diff for these findings (see
    ``test_unanchored_finding_escalation_still_validated_against_diff``).
    """

    def test_unanchored_file_in_tree_is_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "plan.md").write_text("hello")
        finding = _make_finding(file="docs/plan.md", line_start=None, line_end=None)
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), _make_diff(), worktree=tmp_path
        )
        assert accepted == [finding]
        assert rejected == []

    def test_unanchored_file_not_in_tree_still_unknown_file(
        self, tmp_path: Path
    ) -> None:
        # worktree is opted in but the cited file does not exist on disk —
        # the tree check fails, so this falls back to unknown_file exactly
        # like the no-worktree case.
        finding = _make_finding(file="docs/plan.md", line_start=None, line_end=None)
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), _make_diff(), worktree=tmp_path
        )
        assert rejected[0].reason == "unknown_file"

    def test_unanchored_path_traversal_outside_worktree_rejected(
        self, tmp_path: Path
    ) -> None:
        # Proves the containment guard, not just existence: the cited path
        # DOES exist on the real filesystem (a tmp_path sibling), but escapes
        # the worktree root via "../" — must still be unknown_file.
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (tmp_path / "sibling.txt").write_text("secret")
        finding = _make_finding(file="../sibling.txt", line_start=None, line_end=None)
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), _make_diff(), worktree=worktree
        )
        assert rejected[0].reason == "unknown_file"

    def test_unanchored_finding_preserves_reviewer_text(self, tmp_path: Path) -> None:
        (tmp_path / "docs.md").write_text("x")
        finding = _make_finding(
            file="docs.md",
            line_start=None,
            line_end=None,
            summary="custom summary",
            consequence="custom consequence",
            suggested_fix="custom fix",
            evidence="custom evidence",
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), _make_diff(), worktree=tmp_path
        )
        assert not rejected
        assert accepted[0].summary == "custom summary"
        assert accepted[0].consequence == "custom consequence"
        assert accepted[0].suggested_fix == "custom fix"
        assert accepted[0].evidence == "custom evidence"

    def test_unanchored_finding_escalation_still_validated_against_diff(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "docs.md").write_text("x")
        good_esc = _make_escalation(evidence_quote="def broken():")
        good_finding = _make_finding(
            file="docs.md", line_start=None, line_end=None, escalation=good_esc
        )
        accepted, rejected, stripped = validate_reviewer_document(
            _make_reviewer_doc(good_finding), _make_diff(), worktree=tmp_path
        )
        assert not rejected
        assert accepted[0].escalation is not None
        assert not stripped

        bad_esc = _make_escalation(evidence_quote="ghost quote")
        bad_finding = _make_finding(
            file="docs.md", line_start=None, line_end=None, escalation=bad_esc
        )
        accepted2, rejected2, stripped2 = validate_reviewer_document(
            _make_reviewer_doc(bad_finding), _make_diff(), worktree=tmp_path
        )
        assert not rejected2
        assert accepted2[0].escalation is None
        assert len(stripped2) == 1


class TestNoDiffAnchorFindings:
    """#1817: a finding whose remedy lies outside the diff entirely.

    Distinct from #1632's ``"unanchored"`` (a real path on disk that simply
    isn't in this diff): here there is no artifact to point at at all — a
    missing follow-up ticket, an absent acceptance-criterion discharge. The
    marker is an explicit boolean, and ``file`` carries the fixed literal
    ``"N/A"`` so the field stays queryable. Such a finding must never be
    mechanically rejected as ``"unknown_file"``, which is exactly the #1764
    silent-drop this ticket exists to close.
    """

    def test_no_diff_anchor_finding_is_accepted_without_diff_match(self) -> None:
        finding = _make_finding(
            no_diff_anchor=True,
            file="N/A",
            line_start=None,
            line_end=None,
            evidence="acceptance criterion 3 requires a follow-up ticket",
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), _make_diff()
        )
        assert accepted == [finding]
        assert rejected == []

    def test_no_diff_anchor_with_line_anchor_raises_validation_error(self) -> None:
        # A claimed line position is meaningless when the marker asserts there
        # is no diff to anchor it to — fail fast on the producer mistake.
        with pytest.raises(ValidationError):
            Finding.model_validate(
                _finding_kwargs(
                    no_diff_anchor=True, file="N/A", line_start=5, line_end=None
                )
            )
        with pytest.raises(ValidationError):
            Finding.model_validate(
                _finding_kwargs(
                    no_diff_anchor=True, file="N/A", line_start=None, line_end=7
                )
            )

    def test_no_diff_anchor_with_non_na_file_raises_validation_error(self) -> None:
        # #1817 review (2026-08-11): the docstring's "N/A" literal requirement
        # was documented but unenforced — this is the enforcement.
        with pytest.raises(ValidationError):
            Finding.model_validate(
                _finding_kwargs(
                    no_diff_anchor=True,
                    file="src/cw/review_findings.py",
                    line_start=None,
                    line_end=None,
                )
            )

    def test_no_diff_anchor_finding_preserves_reviewer_text(self) -> None:
        finding = _make_finding(
            no_diff_anchor=True,
            file="N/A",
            line_start=None,
            line_end=None,
            summary="custom summary",
            consequence="custom consequence",
            suggested_fix="custom fix",
            evidence="custom evidence",
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), _make_diff()
        )
        assert not rejected
        assert accepted[0].summary == "custom summary"
        assert accepted[0].consequence == "custom consequence"
        assert accepted[0].suggested_fix == "custom fix"
        assert accepted[0].evidence == "custom evidence"
        assert accepted[0].no_diff_anchor is True

    def test_no_diff_anchor_finding_escalation_still_validated_against_diff(
        self,
    ) -> None:
        good_finding = _make_finding(
            no_diff_anchor=True,
            file="N/A",
            line_start=None,
            line_end=None,
            escalation=_make_escalation(evidence_quote="def broken():"),
        )
        accepted, rejected, stripped = validate_reviewer_document(
            _make_reviewer_doc(good_finding), _make_diff()
        )
        assert not rejected
        assert accepted[0].escalation is not None
        assert not stripped

        bad_finding = _make_finding(
            no_diff_anchor=True,
            file="N/A",
            line_start=None,
            line_end=None,
            escalation=_make_escalation(evidence_quote="ghost quote"),
        )
        accepted2, rejected2, stripped2 = validate_reviewer_document(
            _make_reviewer_doc(bad_finding), _make_diff()
        )
        assert not rejected2
        assert accepted2[0].escalation is None
        assert len(stripped2) == 1

    def test_no_diff_anchor_should_fix_also_accepted(self) -> None:
        """Consolidation-level acceptance is severity-agnostic.

        A SHOULD_FIX ``no_diff_anchor`` finding is accepted exactly like a
        MUST_FIX one — the silent-drop side effect is fixed for both. This
        asserts nothing about adjudication: per Decision C2 only a MUST_FIX
        may reach the ``operator_action`` outcome (see
        ``tests/test_review_adjudication.py``).
        """
        finding = _make_finding(
            severity="SHOULD_FIX",
            no_diff_anchor=True,
            file="N/A",
            line_start=None,
            line_end=None,
            evidence="the docs page this change needs does not exist yet",
        )
        accepted, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), _make_diff()
        )
        assert accepted == [finding]
        assert rejected == []

    def test_no_diff_anchor_finding_logs_routing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        finding = _make_finding(
            no_diff_anchor=True, file="N/A", line_start=None, line_end=None
        )
        with caplog.at_level(logging.INFO, logger="cw.review_findings"):
            validate_reviewer_document(_make_reviewer_doc(finding), _make_diff())
        assert "no_diff_anchor" in caplog.text

    def test_pm_reviewer_missing_ticket_finding_not_mechanically_rejected(self) -> None:
        """The literal #1764 regression: a PM finding with no diff artifact.

        Before #1817 this landed in ``rejected`` as ``"unknown_file"`` and
        therefore in ``rejected_must_fix``, hard-blocking the run through the
        #1714 mechanical-reject path with no route to an operator action.
        """
        finding = _make_finding(
            severity="MUST_FIX",
            no_diff_anchor=True,
            file="N/A",
            line_start=None,
            line_end=None,
            summary="ticket AC3's follow-up ticket was never filed",
            evidence=(
                "AC3: a follow-up ticket must exist for the deferred "
                "migration before this ships"
            ),
        )
        verdict = consolidate_verdict(
            [_make_reviewer_doc(finding, reviewer_role="Product Manager Reviewer")],
            _make_diff(),
            "abc1234",
        )
        assert [af.finding for af in verdict.accepted] == [finding]
        assert verdict.rejected == []
        assert verdict.rejected_must_fix == []


# Shared fixture source for TestEnclosingDefSpan and TestAnchorInEnclosingDef.
_ENCLOSING_DEF_SHORT_SOURCE = (
    "def helper():\n"
    "    return 1\n"
    "\n"
    "def target_function(a, b, c, d, e):\n"
    "    x = a + b\n"
    "    y = c + d\n"
    "    return x + y + e\n"
)

# Shared fixture source for TestEnclosingDefAnchor and
# TestLineReferenceValidWorktreeParam. Deliberately spaced so the anchor
# (target_function's def line, 6) sits MORE than _LINE_ANCHOR_TOLERANCE (3)
# lines from every changed line used in those tests — otherwise
# _nearest_added_line's own near-miss tolerance (#1715) would already resolve
# the endpoint and the new enclosing-def fallback would never actually be
# exercised.
_ENCLOSING_DEF_SOURCE = (
    "def helper():\n"  # 1
    "    return 1\n"  # 2
    "\n"  # 3
    "\n"  # 4
    "\n"  # 5
    "def target_function(a, b, c, d, e):\n"  # 6
    "    x = a + b\n"  # 7
    "    y = c + d\n"  # 8
    "    z = x + y\n"  # 9
    "    w = z + e\n"  # 10
    "    v = w * 2\n"  # 11
    "    return v\n"  # 12
)


def _write_enclosing_def_source(tmp_path: Path, source: str) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "mod.py").write_text(source)


class TestEnclosingDefSpan:
    """Pure unit tests of ``_enclosing_def_span`` (#1743): resolve a source
    line to the ``(start, end)`` line span of its innermost enclosing
    function/class, or ``None`` if no such enclosing definition exists.
    """

    def test_line_at_def_itself_returns_span(self) -> None:
        assert _enclosing_def_span(_ENCLOSING_DEF_SHORT_SOURCE, 4) == (4, 7)

    def test_line_inside_body_returns_same_span(self) -> None:
        assert _enclosing_def_span(_ENCLOSING_DEF_SHORT_SOURCE, 6) == (4, 7)

    def test_module_scope_line_returns_none(self) -> None:
        # Line 3 is the blank line between the two top-level defs — module
        # scope, no enclosing function/class.
        assert _enclosing_def_span(_ENCLOSING_DEF_SHORT_SOURCE, 3) is None

    def test_nested_function_innermost_span_wins(self) -> None:
        source = (
            "def outer():\n    def inner():\n        return 1\n    return inner()\n"
        )
        # Line 3 is inside both outer (1-4) and inner (2-3) — inner must win.
        assert _enclosing_def_span(source, 3) == (2, 3)

    def test_class_definition_span_covers_whole_body(self) -> None:
        source = "class Foo:\n    def bar(self):\n        return 1\n"
        assert _enclosing_def_span(source, 1) == (1, 3)

    def test_decorator_line_has_no_enclosing_span(self) -> None:
        source = "@staticmethod\ndef foo():\n    return 1\n"
        assert _enclosing_def_span(source, 1) is None
        assert _enclosing_def_span(source, 2) == (2, 3)

    def test_syntax_error_source_returns_none(self) -> None:
        assert _enclosing_def_span("def foo(:\n    pass\n", 1) is None

    def test_line_past_eof_returns_none(self) -> None:
        assert _enclosing_def_span(_ENCLOSING_DEF_SHORT_SOURCE, 999) is None


class TestAnchorInEnclosingDef:
    """Unit tests of ``_anchor_in_enclosing_def`` (#1743): the I/O-touching
    wrapper that reads *file* under *worktree*, resolves *line*'s enclosing
    def/class span, and checks whether any of *diff*'s changed lines for
    *file* fall inside that span.
    """

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        diff = _make_diff("    y = c + d", files={"src/pkg/mod.py": [6]})
        assert _anchor_in_enclosing_def(diff, tmp_path, "src/pkg/mod.py", 4) is False

    def test_changed_line_inside_span_returns_true(self, tmp_path: Path) -> None:
        _write_enclosing_def_source(tmp_path, _ENCLOSING_DEF_SHORT_SOURCE)
        diff = _make_diff("    y = c + d", files={"src/pkg/mod.py": [6]})
        assert _anchor_in_enclosing_def(diff, tmp_path, "src/pkg/mod.py", 4) is True

    def test_no_changed_line_inside_span_returns_false(self, tmp_path: Path) -> None:
        _write_enclosing_def_source(tmp_path, _ENCLOSING_DEF_SHORT_SOURCE)
        # Line 2 (inside helper(), span 1-2) is changed, but the anchor is
        # target_function's def line (span 4-7) — no overlap.
        diff = _make_diff("    return 1", files={"src/pkg/mod.py": [2]})
        assert _anchor_in_enclosing_def(diff, tmp_path, "src/pkg/mod.py", 4) is False


class TestEnclosingDefAnchor:
    """Integration tests through ``validate_reviewer_document`` (#1743): a
    finding anchored on an enclosing ``def``/``class`` line that is not
    itself changed is no longer mechanically rejected ``invalid_line_reference``
    when a changed line falls inside that definition's span AND a worktree is
    supplied — it instead proceeds to the evidence check, which (since these
    findings don't quote the changed line's real content) currently lands on
    ``evidence_not_in_diff``. That reclassification is an intentional
    side-effect of this ticket; #1738 owns evidence-quote matching itself.
    """

    _CLASS_SOURCE = (
        "class Foo:\n"
        "    def bar(self):\n"
        "        return 1\n"
        "\n"
        "    def baz(self):\n"
        "        return 2\n"
    )

    def test_def_line_anchor_accepted_with_worktree(self, tmp_path: Path) -> None:
        _write_enclosing_def_source(tmp_path, _ENCLOSING_DEF_SOURCE)
        diff = _make_diff("    v = w * 2", files={"src/pkg/mod.py": [11]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=6,
            line_end=6,
            evidence="target_function does too many things",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=tmp_path
        )
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_def_line_anchor_rejected_without_worktree(self, tmp_path: Path) -> None:
        _write_enclosing_def_source(tmp_path, _ENCLOSING_DEF_SOURCE)
        diff = _make_diff("    v = w * 2", files={"src/pkg/mod.py": [11]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=6,
            line_end=6,
            evidence="target_function does too many things",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=None
        )
        assert rejected[0].reason == "invalid_line_reference"

    def test_def_span_with_no_changed_line_inside_still_rejected(
        self, tmp_path: Path
    ) -> None:
        _write_enclosing_def_source(tmp_path, _ENCLOSING_DEF_SOURCE)
        # Changed line 1 sits inside helper()'s span (1-2), not
        # target_function's (6-12) — the anchor's own span has no changed
        # line, so the fallback correctly declines to rescue it.
        diff = _make_diff("def helper():", files={"src/pkg/mod.py": [1]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=6,
            line_end=6,
            evidence="target_function does too many things",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=tmp_path
        )
        assert rejected[0].reason == "invalid_line_reference"

    def test_anchor_with_no_enclosing_def_still_rejected(self, tmp_path: Path) -> None:
        _write_enclosing_def_source(tmp_path, _ENCLOSING_DEF_SOURCE)
        # Line 4 (one of the blank lines between the two top-level defs) has
        # no enclosing function/class at all, regardless of where the changed
        # lines are.
        diff = _make_diff("    v = w * 2", files={"src/pkg/mod.py": [11]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=4,
            line_end=4,
            evidence="module scope finding",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=tmp_path
        )
        assert rejected[0].reason == "invalid_line_reference"

    def test_class_def_anchor_accepted(self, tmp_path: Path) -> None:
        _write_enclosing_def_source(tmp_path, self._CLASS_SOURCE)
        diff = _make_diff("        return 2", files={"src/pkg/mod.py": [6]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=1,
            line_end=1,
            evidence="Foo does too many things",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=tmp_path
        )
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_syntax_error_source_falls_back_to_invalid_line_reference(
        self, tmp_path: Path
    ) -> None:
        _write_enclosing_def_source(tmp_path, "def foo(:\n    pass\n")
        # Changed line (100) is far outside tolerance of the anchor (1), so
        # the fallback is actually exercised (and hits the parse failure).
        diff = _make_diff("    pass", files={"src/pkg/mod.py": [100]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=1,
            line_end=1,
            evidence="foo does too many things",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=tmp_path
        )
        assert rejected[0].reason == "invalid_line_reference"


def _find_function_node(
    tree: ast.AST, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Find the first ``def``/``async def`` node named *name* in *tree*.

    Shared by ``TestEnclosingDefAnchorRealFileRegression._discover_span`` and
    ``TestPromptsGetPurposePromptStructuralFinding._discover_def_line`` (#1764)
    — both discover a real function's span dynamically via ``ast.parse``
    rather than hardcoding line numbers (#1743's discipline).
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    msg = f"{name} not found"
    raise AssertionError(msg)


class TestEnclosingDefAnchorRealFileRegression:
    """Reproduces the ticket's exact evidence: a structural finding anchored
    on ``_run_fix_and_commit``'s real ``def`` line in
    ``src/cw/codex_fix_loop.py``, which is not itself a changed line. The
    function's real span is discovered dynamically via ``ast.parse`` in this
    test's own setup (not the helper under test) so the assertion stays
    correct if the function is refactored — #1743 explicitly rejects
    hardcoding the line numbers observed at plan time.
    """

    def _discover_span(self, repo_root: Path) -> tuple[int, int]:
        source_path = repo_root / "src" / "cw" / "codex_fix_loop.py"
        tree = ast.parse(source_path.read_text())
        node = _find_function_node(tree, "_run_fix_and_commit")
        assert node.end_lineno is not None
        return node.lineno, node.end_lineno

    def _changed_line_beyond_tolerance(self, def_line: int, end_line: int) -> int:
        # Must sit more than _LINE_ANCHOR_TOLERANCE (3) lines from def_line so
        # _nearest_added_line's own near-miss tolerance (#1715) doesn't
        # already resolve the anchor before the new fallback is exercised.
        changed_line = def_line + _LINE_ANCHOR_TOLERANCE + 1
        assert changed_line <= end_line, (
            "_run_fix_and_commit is too short for this regression test's "
            "assumption — pick a line closer to end_line"
        )
        return changed_line

    def test_def_line_anchor_accepted_with_worktree(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        def_line, end_line = self._discover_span(repo_root)
        changed_line = self._changed_line_beyond_tolerance(def_line, end_line)
        diff = _make_diff(
            "some changed line inside the function",
            files={"src/cw/codex_fix_loop.py": [changed_line]},
        )
        finding = _make_finding(
            file="src/cw/codex_fix_loop.py",
            line_start=def_line,
            line_end=def_line,
            evidence="_run_fix_and_commit does too many things",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=repo_root
        )
        assert rejected[0].reason == "evidence_not_in_diff"

    def test_def_line_anchor_rejected_without_worktree(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        def_line, end_line = self._discover_span(repo_root)
        changed_line = self._changed_line_beyond_tolerance(def_line, end_line)
        diff = _make_diff(
            "some changed line inside the function",
            files={"src/cw/codex_fix_loop.py": [changed_line]},
        )
        finding = _make_finding(
            file="src/cw/codex_fix_loop.py",
            line_start=def_line,
            line_end=def_line,
            evidence="_run_fix_and_commit does too many things",
        )
        _, rejected, _ = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=None
        )
        assert rejected[0].reason == "invalid_line_reference"


class TestPromptsGetPurposePromptStructuralFinding:
    """#1764 corroboration: reproduces the ticket's own live #1703 evidence
    (session bf5d88b3) — a structural MUST_FIX anchored on
    ``get_purpose_prompt``'s real ``def`` line in ``src/cw/prompts.py``,
    which is not itself a changed line in the captured #1703 diff. Same
    rejection mode as ``Test9491MustFixCaseReconstruction`` above, on an
    independent function and via the OTHER anchor-resolution sub-path: 9491
    resolves via #1715's plain near-line tolerance, this one only via
    #1743's enclosing-def fallback (the claimed line sits 6 lines from the
    nearest added line, beyond the tolerance of 3).
    """

    _SUMMARY = (
        "get_purpose_prompt now spans 54 lines, exceeding the 50-line function limit."
    )

    def _discover_def_line(self, repo_root: Path) -> int:
        # Discovered dynamically via ast.parse against the real
        # src/cw/prompts.py, not hardcoded — mirrors
        # TestEnclosingDefAnchorRealFileRegression's discipline.
        source_path = repo_root / "src" / "cw" / "prompts.py"
        tree = ast.parse(source_path.read_text())
        return _find_function_node(tree, "get_purpose_prompt").lineno

    def _finding(self, def_line: int) -> Finding:
        return Finding(
            severity="MUST_FIX",
            file="src/cw/prompts.py",
            line_start=def_line,
            line_end=None,
            summary=self._SUMMARY,
            consequence="x",
            suggested_fix="x",
            evidence=self._SUMMARY,
            confidence="HIGH",
        )

    def test_1703_line_reference_invalid_without_worktree(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        def_line = self._discover_def_line(repo_root)
        diff = _pr1703_captured_diff()
        finding = self._finding(def_line)
        # Nearest added line (122) is 6 away from the claimed def line,
        # beyond _LINE_ANCHOR_TOLERANCE (3).
        assert _line_reference_valid(diff, finding, worktree=None) is False

    def test_1703_line_reference_valid_via_enclosing_def_with_worktree(
        self,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        def_line = self._discover_def_line(repo_root)
        diff = _pr1703_captured_diff()
        assert _anchor_in_enclosing_def(diff, repo_root, "src/cw/prompts.py", def_line)
        finding = self._finding(def_line)
        assert _line_reference_valid(diff, finding, worktree=repo_root) is True

    def test_1703_classified_evidence_not_in_diff_matching_production(
        self,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        def_line = self._discover_def_line(repo_root)
        diff = _pr1703_captured_diff()
        finding = self._finding(def_line)
        changed = frozenset(diff.files)
        assert (
            _classify_finding(finding, diff, changed, worktree=repo_root)
            == "evidence_not_in_diff"
        )

    def test_1703_rejected_via_consolidate_verdict(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        def_line = self._discover_def_line(repo_root)
        diff = _pr1703_captured_diff()
        finding = self._finding(def_line)
        doc = _make_reviewer_doc(finding)
        verdict = consolidate_verdict(
            [doc], diff, reviewed_sha="535fbd23", worktree=repo_root
        )
        assert verdict.blocking is False
        assert verdict.must_fix == []
        assert len(verdict.rejected_must_fix) == 1
        assert verdict.rejected_must_fix[0].reason == "evidence_not_in_diff"
        assert verdict.rejected_must_fix[0].raw["severity"] == "MUST_FIX"


class TestLineReferenceValidWorktreeParam:
    """Direct unit tests of ``_line_reference_valid``'s new ``worktree``
    parameter and ``_classify_finding``'s pass-through of it (#1743).
    """

    def test_line_reference_valid_defaults_to_no_worktree(self, tmp_path: Path) -> None:
        _write_enclosing_def_source(tmp_path, _ENCLOSING_DEF_SOURCE)
        diff = _make_diff("    v = w * 2", files={"src/pkg/mod.py": [11]})
        finding = _make_finding(
            file="src/pkg/mod.py", line_start=6, line_end=6, evidence="x"
        )
        assert _line_reference_valid(diff, finding) is False

    def test_line_reference_valid_with_worktree_rescues_def_anchor(
        self, tmp_path: Path
    ) -> None:
        _write_enclosing_def_source(tmp_path, _ENCLOSING_DEF_SOURCE)
        diff = _make_diff("    v = w * 2", files={"src/pkg/mod.py": [11]})
        finding = _make_finding(
            file="src/pkg/mod.py", line_start=6, line_end=6, evidence="x"
        )
        assert _line_reference_valid(diff, finding, tmp_path) is True

    def test_classify_finding_passes_worktree_through(self, tmp_path: Path) -> None:
        _write_enclosing_def_source(tmp_path, _ENCLOSING_DEF_SOURCE)
        diff = _make_diff("    v = w * 2", files={"src/pkg/mod.py": [11]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=6,
            line_end=6,
            evidence="target_function does too many things",
        )
        changed = frozenset(diff.files)
        # evidence doesn't match the changed line's real text, so the def-line
        # anchor is rescued and classification proceeds to the evidence check.
        assert (
            _classify_finding(finding, diff, changed, tmp_path)
            == "evidence_not_in_diff"
        )
        assert _classify_finding(finding, diff, changed, None) == (
            "invalid_line_reference"
        )


class TestEscalationStripOnInvalidEvidence:
    def test_bad_quote_stripped_finding_survives(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        esc = _make_escalation(
            target_reviewer="Perf Reviewer", evidence_quote="ghost quote"
        )
        f = _make_finding(severity="MUST_FIX", escalation=esc)
        doc = _make_reviewer_doc(f, reviewer_role="Security Reviewer")
        with caplog.at_level(logging.WARNING):
            accepted, rejected, stripped = validate_reviewer_document(doc, _make_diff())
        assert not rejected
        assert len(accepted) == 1
        assert accepted[0].escalation is None
        assert len(stripped) == 1
        se = stripped[0]
        assert se.reason == "escalation_evidence_not_in_diff"
        assert se.reviewer_role == "Security Reviewer"
        assert se.finding_index == 0
        assert se.target_reviewer == "Perf Reviewer"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "Security Reviewer" in msg
        assert "Perf Reviewer" in msg
        assert "0" in msg
        # Counted in must_fix_initial despite the strip.
        review = derive_review_counts(
            dedupe_findings([("Security Reviewer", accepted[0])])
        )
        assert review.must_fix_initial == 1

    def test_valid_quote_preserved(self, caplog: pytest.LogCaptureFixture) -> None:
        esc = _make_escalation(evidence_quote="def broken():")
        f = _make_finding(escalation=esc)
        with caplog.at_level(logging.WARNING):
            accepted, _, stripped = validate_reviewer_document(
                _make_reviewer_doc(f), _make_diff()
            )
        assert accepted[0].escalation is not None
        assert not stripped
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_no_escalation_untouched(self, caplog: pytest.LogCaptureFixture) -> None:
        f = _make_finding(escalation=None)
        with caplog.at_level(logging.WARNING):
            accepted, _, stripped = validate_reviewer_document(
                _make_reviewer_doc(f), _make_diff()
            )
        assert accepted[0].escalation is None
        assert not stripped
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_rejected_finding_never_reaches_escalation_check(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Finding is itself rejected (blank evidence via model_construct) AND
        # carries an escalation with a bad quote — no strip is produced.
        esc = _make_escalation(evidence_quote="ghost")
        bad = Finding.model_construct(**_finding_kwargs(evidence="   ", escalation=esc))
        with caplog.at_level(logging.WARNING):
            accepted, rejected, stripped = validate_reviewer_document(
                _make_reviewer_doc(bad), _make_diff()
            )
        assert not accepted
        assert rejected[0].reason == "missing_evidence"
        assert not stripped
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_quote_matches_full_diff_not_only_added_lines(self) -> None:
        # A context/removed line (no + prefix) still counts for the quote check.
        diff = _make_diff(extra_text="-removed_context_line = 1")
        esc = _make_escalation(evidence_quote="removed_context_line = 1")
        f = _make_finding(escalation=esc)
        accepted, _, stripped = validate_reviewer_document(_make_reviewer_doc(f), diff)
        assert accepted[0].escalation is not None
        assert not stripped


class TestDedupeFindings:
    def test_two_reviewers_merge_to_one(self) -> None:
        f1 = _make_finding()
        f2 = _make_finding()
        merged = dedupe_findings([("Reviewer A", f1), ("Reviewer B", f2)])
        assert len(merged) == 1
        assert merged[0].reviewers == ["Reviewer A", "Reviewer B"]

    def test_non_duplicates_not_merged(self) -> None:
        f1 = _make_finding(line_start=10, line_end=10)
        f2 = _make_finding(line_start=20, line_end=20)
        merged = dedupe_findings([("A", f1), ("B", f2)])
        assert len(merged) == 2

    def test_deterministic_order_across_permutations(self) -> None:
        f1 = _make_finding(line_start=10, line_end=10)
        f2 = _make_finding(line_start=20, line_end=20, evidence="def broken():")
        order1 = [
            af.finding.line_start for af in dedupe_findings([("A", f1), ("B", f2)])
        ]
        order2 = [
            af.finding.line_start for af in dedupe_findings([("B", f2), ("A", f1)])
        ]
        assert order1 == order2

    def test_escalation_non_null_wins_when_one_side_set(self) -> None:
        esc = _make_escalation(evidence_quote="def broken():")
        with_esc = _make_finding(escalation=esc)
        without = _make_finding(escalation=None)
        merged = dedupe_findings([("A", without), ("B", with_esc)])
        assert len(merged) == 1
        assert merged[0].finding.escalation is not None

    def test_tiebreak_lowest_role_when_neither_escalates(self) -> None:
        f_a = _make_finding(summary="from A")
        f_b = _make_finding(summary="from B")
        forward = dedupe_findings([("Alpha", f_a), ("Zeta", f_b)])
        backward = dedupe_findings([("Zeta", f_b), ("Alpha", f_a)])
        assert forward[0].finding.summary == "from A"
        assert backward[0].finding.summary == "from A"

    def test_tiebreak_lowest_role_when_both_escalate(self) -> None:
        esc = _make_escalation(evidence_quote="def broken():")
        f_a = _make_finding(summary="from A", escalation=esc)
        f_b = _make_finding(summary="from B", escalation=esc)
        merged = dedupe_findings([("Zeta", f_b), ("Alpha", f_a)])
        assert merged[0].finding.summary == "from A"


class TestDeriveReviewCounts:
    def test_counts_by_severity(self) -> None:
        findings = [
            AcceptedFinding(
                finding=_make_finding(severity="MUST_FIX"), reviewers=["a"]
            ),
            AcceptedFinding(
                finding=_make_finding(severity="SHOULD_FIX"), reviewers=["a"]
            ),
        ]
        review = derive_review_counts(findings, fix_cycles_used=2, agents_run=3)
        assert review.must_fix_initial == 1
        assert review.should_fix == 1
        assert review.deferred == 0
        assert review.fix_cycles_used == 2
        assert review.agents_run == 3

    def test_derive_review_counts_threads_rejected_count_and_by_severity(self) -> None:
        # #2000 (A4): the two new parameters are pass-throughs on the same
        # footing as agents_run -- pre-computed by consolidate_verdict, carried
        # onto the Review that becomes AUTO_DEV_RESULT.review.
        review = derive_review_counts(
            [],
            rejected_count=3,
            rejected_count_by_severity={"SHOULD_FIX": 2, "NIT": 1},
        )
        assert review.rejected_count == 3
        assert review.rejected_count_by_severity == {"SHOULD_FIX": 2, "NIT": 1}

    def test_derive_review_counts_rejected_count_defaults_to_zero_when_omitted(
        self,
    ) -> None:
        # #2000: every pre-existing no-kwarg call site keeps working unchanged.
        review = derive_review_counts(
            [
                AcceptedFinding(
                    finding=_make_finding(severity="MUST_FIX"), reviewers=["a"]
                )
            ]
        )
        assert review.rejected_count == 0
        assert review.rejected_count_by_severity == {}

    def test_must_fix_deferred_counts_as_deferred(self) -> None:
        findings = [
            AcceptedFinding(
                finding=_make_finding(severity="MUST_FIX"),
                reviewers=["a"],
                disposition="deferred",
            ),
        ]
        review = derive_review_counts(findings)
        assert review.must_fix_initial == 0
        assert review.deferred == 1

    def test_should_fix_deferred_counts_as_deferred(self) -> None:
        findings = [
            AcceptedFinding(
                finding=_make_finding(severity="SHOULD_FIX"),
                reviewers=["a"],
                disposition="deferred",
            ),
        ]
        review = derive_review_counts(findings)
        assert review.should_fix == 0
        assert review.deferred == 1

    def test_nit_deferred_excluded_from_deferred_count(self) -> None:
        # NIT/PRINCIPLE never touch any of the 3 gate-feeding aggregates,
        # regardless of disposition — deferred is severity-filtered too.
        findings = [
            AcceptedFinding(
                finding=_make_finding(severity="NIT"),
                reviewers=["a"],
                disposition="deferred",
            ),
        ]
        review = derive_review_counts(findings)
        assert review.deferred == 0
        assert review.must_fix_initial == 0
        assert review.should_fix == 0

    def test_principle_deferred_excluded_from_deferred_count(self) -> None:
        findings = [
            AcceptedFinding(
                finding=_make_finding(severity="PRINCIPLE"),
                reviewers=["a"],
                disposition="deferred",
            ),
        ]
        review = derive_review_counts(findings)
        assert review.deferred == 0
        assert review.must_fix_initial == 0
        assert review.should_fix == 0


class TestConsolidateVerdict:
    def test_base_verdict(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX"), reviewer_role="Reviewer A"
        )
        verdict = consolidate_verdict([doc], diff, reviewed_sha="abc123")
        assert verdict.blocking is True
        assert len(verdict.must_fix) == 1
        assert verdict.reviewed_sha == "abc123"
        assert len(verdict.agents_run) == 1
        assert verdict.agents_run[0].reviewer_role == "Reviewer A"
        assert verdict.review.agents_run == 1

    def test_non_blocking_when_no_must_fix(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        assert verdict.blocking is False
        assert verdict.must_fix == []

    def test_unanchored_must_fix_finding_blocks_via_worktree(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "docs.md").write_text("x")
        finding = _make_finding(
            severity="MUST_FIX", file="docs.md", line_start=None, line_end=None
        )
        doc = _make_reviewer_doc(finding)
        verdict = consolidate_verdict(
            [doc], _make_diff(), reviewed_sha="sha", worktree=tmp_path
        )
        assert verdict.blocking is True
        assert len(verdict.must_fix) == 1
        assert verdict.rejected == []
        assert verdict.review.must_fix_initial == 1

    def test_unanchored_without_worktree_still_rejected(self, tmp_path: Path) -> None:
        # Same finding, no worktree kwarg passed — proves the relaxation is
        # strictly opt-in even when the cited file genuinely exists on disk.
        (tmp_path / "docs.md").write_text("x")
        finding = _make_finding(
            severity="MUST_FIX", file="docs.md", line_start=None, line_end=None
        )
        doc = _make_reviewer_doc(finding)
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        assert verdict.blocking is False
        assert verdict.rejected[0].reason == "unknown_file"

    def test_mechanically_rejected_must_fix_populates_rejected_must_fix_field(
        self, tmp_path: Path
    ) -> None:
        # #1714: the fleet reproduction. A MUST_FIX rejected for a MECHANICAL
        # reason (here unknown_file) is dropped before adjudication, so
        # `blocking` stays False by design (R4 -- an unreliable anchor must
        # never enter the autofix loop). `rejected_must_fix` is the separate
        # signal that says "something MUST_FIX-shaped was silently dropped".
        (tmp_path / "docs.md").write_text("x")
        finding = _make_finding(
            severity="MUST_FIX", file="docs.md", line_start=None, line_end=None
        )
        doc = _make_reviewer_doc(finding)
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        assert verdict.blocking is False
        assert verdict.must_fix == []
        assert len(verdict.rejected_must_fix) == 1
        assert verdict.rejected_must_fix[0].reason == "unknown_file"
        assert verdict.rejected_must_fix[0].raw["severity"] == "MUST_FIX"

    def test_should_fix_mechanical_rejection_does_not_populate_rejected_must_fix(
        self,
    ) -> None:
        # #1714 AC#4: only MUST_FIX-severity rejections raise the new signal;
        # a mechanically-rejected SHOULD_FIX stays purely informational.
        finding = _make_finding(
            severity="SHOULD_FIX", file="docs.md", line_start=None, line_end=None
        )
        doc = _make_reviewer_doc(finding)
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        assert verdict.rejected[0].reason == "unknown_file"
        assert verdict.rejected_must_fix == []

    def test_rejected_count_totals_across_all_severities(self) -> None:
        # #2000: the all-severity tally, across every reviewer document -- the
        # number that makes "PROCEED manufactured by deletion" impossible.
        doc_a = _make_reviewer_doc(
            _make_finding(severity="SHOULD_FIX", file="src/cw/other.py"),
            _make_finding(severity="NIT"),
            reviewer_role="Reviewer A",
        )
        doc_b = _make_reviewer_doc(
            _make_finding(severity="NIT", file="src/cw/other.py"),
            reviewer_role="Reviewer B",
        )
        verdict = consolidate_verdict([doc_a, doc_b], _make_diff(), reviewed_sha="sha")
        assert verdict.rejected_count == 2
        assert verdict.rejected_count_by_severity == {"SHOULD_FIX": 1, "NIT": 1}
        assert verdict.rejected_must_fix == []

    def test_rejected_count_includes_must_fix_rejections_too(self) -> None:
        # #2000: additive, not a replacement -- a MUST_FIX rejection feeds the
        # new counters AND keeps populating the #1714 signal unchanged.
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", file="src/cw/other.py"),
            _make_finding(severity="SHOULD_FIX", file="src/cw/other.py"),
        )
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        assert verdict.rejected_count == 2
        assert verdict.rejected_count_by_severity == {"MUST_FIX": 1, "SHOULD_FIX": 1}
        assert len(verdict.rejected_must_fix) == 1
        assert verdict.rejected_must_fix[0].raw["severity"] == "MUST_FIX"

    def test_zero_rejections_defaults_rejected_count_to_zero(self) -> None:
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding(severity="NIT"))],
            _make_diff(),
            reviewed_sha="sha",
        )
        assert verdict.rejected_count == 0
        assert verdict.rejected_count_by_severity == {}
        assert verdict.review.rejected_count == 0
        assert verdict.review.rejected_count_by_severity == {}

    def test_consolidate_verdict_review_rejected_count_matches_top_level(self) -> None:
        # #2000 (A4): computed once, threaded into both the ReviewVerdict and
        # the nested Review that becomes AUTO_DEV_RESULT.review -- identical by
        # construction, not by convention.
        doc = _make_reviewer_doc(
            _make_finding(severity="SHOULD_FIX", file="src/cw/other.py"),
            _make_finding(severity="NIT"),
        )
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        assert verdict.rejected_count == 1
        assert verdict.review.rejected_count == verdict.rejected_count
        assert verdict.rejected_count_by_severity == {"SHOULD_FIX": 1}
        assert (
            verdict.review.rejected_count_by_severity
            == verdict.rejected_count_by_severity
        )

    def test_rejected_must_fix_keyed_on_category_not_enumerated_reason(self) -> None:
        # #1714 AC#3: the selection is keyed on the finding's SEVERITY, never on
        # an enumerated set of RejectedFindingReason values -- so a reason value
        # that does not exist today is covered by construction. model_construct
        # bypasses the Literal so a synthetic reason can be exercised at all.
        synthetic = RejectedFinding.model_construct(
            raw=_finding_kwargs(severity="MUST_FIX"),
            reviewer_role="Test Reviewer",
            reason="a_synthetic_reason_never_seen_before",
            detail="",
        )
        benign = RejectedFinding.model_construct(
            raw=_finding_kwargs(severity="NIT"),
            reviewer_role="Test Reviewer",
            reason="a_synthetic_reason_never_seen_before",
            detail="",
        )
        assert _select_rejected_must_fix([synthetic, benign]) == [synthetic]

    def test_mixed_blocking_and_rejected_must_fix(self) -> None:
        # #1714: the two signals are independent and can coexist -- an accepted
        # MUST_FIX still blocks while a mechanically-rejected one is reported.
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX"),
            _make_finding(
                severity="MUST_FIX",
                file="not/in/diff.py",
                line_start=None,
                line_end=None,
                summary="dropped one",
            ),
        )
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        assert verdict.blocking is True
        assert len(verdict.must_fix) == 1
        assert len(verdict.rejected_must_fix) == 1

    def test_aggregate_near_line_and_multiline_via_consolidate_verdict(self) -> None:
        # #1715 integration: three findings through the full
        # consolidate_verdict pipeline. (1) file not in diff, no worktree ->
        # still unknown_file (#1632 mechanism untouched). (2) near-line
        # anchor (distance 2, within tolerance) with matching evidence ->
        # accepted. (3) file-level multiline evidence with no diff markers,
        # matched against raw "+"-prefixed file_diffs text via normalization
        # -> accepted. MUST fail red pre-fix (findings 2 and 3 both rejected
        # under exact-match/raw-substring behavior).
        diff = _make_diff(
            "def broken():",
            "line one",
            "line two",
            files={"src/cw/foo.py": [10], "src/cw/bar.py": [20, 21]},
        )
        findings = [
            _make_finding(
                file="src/cw/other.py",
                line_start=None,
                line_end=None,
                evidence="whatever",
            ),
            _make_finding(
                file="src/cw/foo.py",
                line_start=12,
                line_end=12,
                evidence="def broken():",
            ),
            _make_finding(
                file="src/cw/bar.py",
                line_start=None,
                line_end=None,
                evidence="line one\nline two",
            ),
        ]
        doc = _make_reviewer_doc(*findings, reviewer_role="Reviewer A")
        verdict = consolidate_verdict([doc], diff, "deadbeef")
        assert len(verdict.rejected) == 1
        assert verdict.rejected[0].reason == "unknown_file"
        assert len(verdict.accepted) == 2

    def test_aggregate_hunk_context_and_negative_control_via_consolidate_verdict(
        self,
    ) -> None:
        # #1738 integration, mirroring
        # test_aggregate_near_line_and_multiline_via_consolidate_verdict's
        # shape: four findings through the full consolidate_verdict pipeline
        # combining (1) the real #1729 hunk-context finding (mode 4, #1738's
        # fix), (2) a negative control (fabricated evidence genuinely absent
        # from the file, at any offset -- must stay rejected), and (3) the
        # two existing #1715 fixtures (near-line anchor, multiline-prefix
        # normalization) -- unaffected by this fix, re-run here to prove the
        # widenings compose cleanly.
        #
        # Finding (2) was originally #1738's own negative control: real
        # CONTEXT-line content from elsewhere in the same file
        # ("from cw.auto_dev_result import (", genuinely at line 9511),
        # claimed at the #1729 finding's 9522-9527 range -- proving the
        # #1738 widened window was still bounded to the claimed window, not
        # a whole-file search. #2019 adds a SEPARATE unbounded rescue one
        # gate later (_classify_mislocated_finding), which DOES search the
        # whole file and would now accept that exact fixture (see
        # TestValidateReviewerDocument.test_hunk_context_window_unrelated_
        # line_now_content_rescued for the dedicated coverage of that flip), so
        # it no longer serves as a negative control here -- swapped for
        # fabricated evidence instead, to keep proving something stays
        # rejected in the aggregate.
        pr1729_diff = _pr1729_captured_diff()
        legacy_diff = _make_diff(
            "def broken():",
            "line one",
            "line two",
            files={"src/cw/foo.py": [10], "src/cw/bar.py": [20, 21]},
        )
        diff = CapturedDiff(
            text=pr1729_diff.text + "\n" + legacy_diff.text,
            files={**pr1729_diff.files, **legacy_diff.files},
            file_diffs={**pr1729_diff.file_diffs, **legacy_diff.file_diffs},
            file_line_text={
                **pr1729_diff.file_line_text,
                **legacy_diff.file_line_text,
            },
            file_window_text={
                **pr1729_diff.file_window_text,
                **legacy_diff.file_window_text,
            },
        )
        findings = [
            Finding(**_PR1729_REJECTED_FINDING_KWARGS),
            _make_finding(
                file="tests/test_dispatch.py",
                line_start=9522,
                line_end=9527,
                evidence="fabricated absent text not in diff anywhere",
            ),
            _make_finding(
                file="src/cw/foo.py",
                line_start=12,
                line_end=12,
                evidence="def broken():",
            ),
            _make_finding(
                file="src/cw/bar.py",
                line_start=None,
                line_end=None,
                evidence="line one\nline two",
            ),
        ]
        doc = _make_reviewer_doc(*findings, reviewer_role="Reviewer A")
        verdict = consolidate_verdict([doc], diff, "deadbeef")
        assert len(verdict.accepted) == 3
        assert len(verdict.rejected) == 1
        assert verdict.rejected[0].reason == "evidence_not_in_diff"


class TestConsolidateVerdictDetail:
    """#1775: a reviewer document's `detail` copies onto its ReviewerRunRecord.

    Before this fix, `consolidate_verdict` read every field of
    `ReviewerFindingsDocument` except `detail` when building each
    `ReviewerRunRecord` — the degraded-reviewer reason a prompt writes into
    `detail` (per .claude/commands/auto-dev-review.md) parsed correctly but
    was dropped before it ever reached the persisted verdict.
    """

    def test_degraded_document_detail_lands_on_matching_run_record(self) -> None:
        doc = _make_reviewer_doc(
            status="degraded",
            detail="sandbox lacked filesystem access",
            reviewer_role="Reviewer A",
        )
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        assert verdict.agents_run[0].detail == "sandbox lacked filesystem access"

    def test_ok_document_detail_also_lands_on_run_record(self) -> None:
        # The copy is unconditional on status, not degraded-special-cased.
        doc = _make_reviewer_doc(detail="reviewed; no issues found.")
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="sha")
        assert verdict.agents_run[0].detail == "reviewed; no issues found."

    def test_degraded_document_with_blank_detail_rejected_at_construction(
        self,
    ) -> None:
        # #1806: a degraded status with no stated reason used to be treated
        # as a real, non-erroring state that survived consolidate_verdict as
        # an empty string -- that was exactly the bug #1806 closes. It is now
        # rejected by ReviewerFindingsDocument's own validator before
        # consolidate_verdict ever sees it.
        with pytest.raises(ValidationError):
            _make_reviewer_doc(status="degraded", detail="")

    def test_failed_reviewer_run_failure_record_has_empty_detail(self) -> None:
        # ReviewerRunFailure carries no detail concept -- a role recorded
        # only via failed_reviewers (no matching document) stays at the
        # ReviewerRunRecord default, by design (out of scope for #1775).
        verdict = consolidate_verdict(
            [],
            _make_diff(),
            reviewed_sha="sha",
            failed_reviewers=[ReviewerRunFailure(role="Solo", reason="crash")],
        )
        assert verdict.agents_run[0].detail == ""


class TestConsolidateVerdictFailedReviewers:
    def test_default_no_failed_reviewers(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding())
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        assert len(verdict.agents_run) == 1

    def test_failed_reviewer_appends_record(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(), reviewer_role="Reviewer A")
        verdict = consolidate_verdict(
            [doc],
            diff,
            reviewed_sha="sha",
            failed_reviewers=[
                ReviewerRunFailure(role="Perf Reviewer", reason="timeout")
            ],
        )
        # The failed reviewer is still RECORDED in the agents_run list (audit
        # trail)...
        assert len(verdict.agents_run) == 2
        failed = [r for r in verdict.agents_run if r.status == "failed"]
        assert len(failed) == 1
        assert failed[0].reviewer_role == "Perf Reviewer"
        assert failed[0].finding_count == 0
        # ...but excluded from the countable review.agents_run int — only
        # roles that actually produced a document count (standing binding
        # decision, #1236).
        assert verdict.review.agents_run == 1

    def test_failed_reviewer_without_document(self) -> None:
        diff = _make_diff()
        verdict = consolidate_verdict(
            [],
            diff,
            reviewed_sha="sha",
            failed_reviewers=[ReviewerRunFailure(role="Solo", reason="crash")],
        )
        assert len(verdict.agents_run) == 1
        assert verdict.agents_run[0].status == "failed"
        # Zero documents produced -> review.agents_run is 0, not 1.
        assert verdict.review.agents_run == 0

    def test_stripped_escalations_union_in_document_order(self) -> None:
        diff = _make_diff()
        esc = _make_escalation(evidence_quote="ghost")
        doc1 = _make_reviewer_doc(_make_finding(escalation=esc), reviewer_role="R1")
        doc2 = _make_reviewer_doc(_make_finding(escalation=esc), reviewer_role="R2")
        verdict = consolidate_verdict([doc1, doc2], diff, reviewed_sha="sha")
        assert len(verdict.stripped_escalations) == 2
        assert verdict.stripped_escalations[0].reviewer_role == "R1"
        assert verdict.stripped_escalations[1].reviewer_role == "R2"


class TestConsolidateVerdictPreValidationRejected:
    """#2029: parse-time rejects join the existing #1714/#2000 machinery."""

    def _schema_invalid_must_fix(self) -> RejectedFinding:
        return RejectedFinding(
            raw=dict(_finding_kwargs(severity="MUST_FIX")),
            reviewer_role="Code Quality Reviewer",
            reason="schema_invalid",
            detail="evidence: Field required",
        )

    def test_pre_validation_reject_force_blocks_via_rejected_must_fix(self) -> None:
        rf = self._schema_invalid_must_fix()
        verdict = consolidate_verdict(
            [], _make_diff(), reviewed_sha="sha", pre_validation_rejected=[rf]
        )
        assert verdict.rejected == [rf]
        assert verdict.rejected_must_fix == [rf]
        # R4 (#1714): the parked signal never flips `blocking` — an unparseable
        # finding's anchor is exactly what must not reach the autofix loop.
        assert verdict.blocking is False
        assert verdict.rejected_count == 1
        assert verdict.rejected_count_by_severity == {"MUST_FIX": 1}

    def test_pre_validation_rejects_precede_per_document_rejects(self) -> None:
        rf = self._schema_invalid_must_fix()
        doc = _make_reviewer_doc(
            _make_finding(
                severity="SHOULD_FIX",
                file="src/cw/never_in_the_diff.py",
                line_start=None,
                line_end=None,
            )
        )
        verdict = consolidate_verdict(
            [doc], _make_diff(), reviewed_sha="sha", pre_validation_rejected=[rf]
        )
        assert [r.reason for r in verdict.rejected] == [
            "schema_invalid",
            "unknown_file",
        ]
        assert verdict.rejected_count == 2
        assert verdict.rejected_count_by_severity == {"MUST_FIX": 1, "SHOULD_FIX": 1}

    def test_default_none_is_byte_identical_to_omitting_it(self) -> None:
        doc = _make_reviewer_doc(_make_finding(severity="MUST_FIX"))
        diff = _make_diff()
        omitted = consolidate_verdict([doc], diff, reviewed_sha="sha")
        explicit = consolidate_verdict(
            [doc], diff, reviewed_sha="sha", pre_validation_rejected=None
        )
        assert omitted.model_dump() == explicit.model_dump()
        assert omitted.run_failures_with_should_fix_discards == []


class TestReviewerRunFailureDiscardedFindings:
    """#2029: the residual whole-document discard tally and its gate."""

    def test_discard_fields_default_to_zero_and_empty(self) -> None:
        failure = ReviewerRunFailure(role="X", reason="schema_mismatch")
        assert failure.discarded_finding_count == 0
        assert failure.discarded_finding_severities == {}

    def test_discard_fields_round_trip(self) -> None:
        failure = ReviewerRunFailure(
            role="X",
            reason="schema_mismatch",
            discarded_finding_count=2,
            discarded_finding_severities={"MUST_FIX": 1, "NIT": 1},
        )
        assert ReviewerRunFailure.model_validate_json(failure.model_dump_json()) == (
            failure
        )

    def test_should_fix_discard_is_selected_onto_the_verdict(self) -> None:
        failure = ReviewerRunFailure(
            role="Perf Reviewer",
            reason="codex_review_unparseable",
            discarded_finding_count=1,
            discarded_finding_severities={"SHOULD_FIX": 1},
        )
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding(severity="NIT"))],
            _make_diff(),
            reviewed_sha="sha",
            failed_reviewers=[failure],
        )
        assert verdict.run_failures_with_should_fix_discards == [failure]

    def test_nit_only_discard_stays_below_the_threshold(self) -> None:
        failure = ReviewerRunFailure(
            role="Perf Reviewer",
            reason="codex_review_unparseable",
            discarded_finding_count=3,
            discarded_finding_severities={"NIT": 3},
        )
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding(severity="NIT"))],
            _make_diff(),
            reviewed_sha="sha",
            failed_reviewers=[failure],
        )
        assert verdict.run_failures_with_should_fix_discards == []

    def test_zero_count_gating_severity_does_not_select(self) -> None:
        # A tally that carries the key but counted nothing is not a discard.
        failure = ReviewerRunFailure(
            role="Perf Reviewer",
            reason="codex_review_unparseable",
            discarded_finding_severities={"MUST_FIX": 0},
        )
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding(severity="NIT"))],
            _make_diff(),
            reviewed_sha="sha",
            failed_reviewers=[failure],
        )
        assert verdict.run_failures_with_should_fix_discards == []

    def test_failure_without_discards_is_not_selected(self) -> None:
        failure = ReviewerRunFailure(role="Perf Reviewer", reason="codex_timeout")
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding(severity="NIT"))],
            _make_diff(),
            reviewed_sha="sha",
            failed_reviewers=[failure],
        )
        assert verdict.run_failures_with_should_fix_discards == []


class TestBestEffortDiscardedTally:
    """#2029: the tally must survive whatever the broken payload turns out to be.

    Its input is by definition a document that already failed to parse, so every
    step of the walk is exercised directly here — several of these shapes never
    reach it through the codex path (a payload that is not valid JSON classifies
    as ``invalid_json`` upstream and is never tallied), and a defensive branch
    that only production can reach is a branch nothing has ever run.
    """

    def test_counts_findings_by_claimed_severity(self) -> None:
        raw = json.dumps(
            {
                "status": "ok",
                "findings": [
                    {"severity": "MUST_FIX"},
                    {"severity": "NIT"},
                    {"severity": "NIT"},
                ],
            }
        )
        assert _best_effort_discarded_tally(raw) == (3, {"MUST_FIX": 1, "NIT": 2})

    def test_accepts_an_already_decoded_payload(self) -> None:
        payload = {"findings": [{"severity": "SHOULD_FIX"}]}
        assert _best_effort_discarded_tally(payload) == (1, {"SHOULD_FIX": 1})

    def test_undecodable_json_tallies_nothing(self) -> None:
        assert _best_effort_discarded_tally("not json{{") == (0, {})

    def test_non_dict_payload_tallies_nothing(self) -> None:
        assert _best_effort_discarded_tally(json.dumps([1, 2, 3])) == (0, {})
        assert _best_effort_discarded_tally(None) == (0, {})

    def test_non_list_findings_key_tallies_nothing(self) -> None:
        assert _best_effort_discarded_tally({"findings": "nope"}) == (0, {})
        assert _best_effort_discarded_tally({"status": "ok"}) == (0, {})

    def test_unreadable_severities_bucket_as_unknown(self) -> None:
        # A finding with no severity, and an item that is not a dict at all —
        # both still happened, so both are counted rather than dropped.
        payload = {"findings": [{"file": "a.py"}, "not a finding"]}
        assert _best_effort_discarded_tally(payload) == (2, {"unknown": 2})


class TestConsolidateVerdictFixCycles:
    """The #1392 fix_cycles_used threading through consolidate_verdict."""

    def test_fix_cycles_used_threads_into_review(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="MUST_FIX"))
        verdict = consolidate_verdict(
            [doc], diff, reviewed_sha="sha", fix_cycles_used=3
        )
        assert verdict.review.fix_cycles_used == 3

    def test_fix_cycles_used_defaults_to_zero_when_omitted(self) -> None:
        # Regression guard: the pre-#1392 call shape (no fix_cycles_used) still
        # yields fix_cycles_used=0, so every existing single-pass caller is
        # byte-identical.
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        assert verdict.review.fix_cycles_used == 0

    def test_derive_review_counts_default_unchanged(self) -> None:
        # derive_review_counts still defaults fix_cycles_used to 0 for the
        # hardcoded-zero Review(...) constructions in local_runner.
        findings = [
            AcceptedFinding(finding=_make_finding(severity="MUST_FIX"), reviewers=["a"])
        ]
        review = derive_review_counts(findings)
        assert review.fix_cycles_used == 0
        assert review.must_fix_initial == 1


class TestWriteReviewVerdictArtifact:
    def test_atomic_write_round_trips(self, tmp_path: Path) -> None:
        diff = _make_diff()
        esc = _make_escalation(evidence_quote="ghost")
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", escalation=esc),
            reviewer_role="R1",
        )
        verdict = consolidate_verdict([doc], diff, reviewed_sha="deadbeef")
        path = tmp_path / "review-verdict.json"
        write_review_verdict(verdict, path)
        data = json.loads(path.read_text())
        # #1108's 3 required keys present.
        assert "blocking" in data
        assert "must_fix" in data
        assert "reviewed_sha" in data
        assert data["reviewed_sha"] == "deadbeef"
        # Superset round-trips.
        assert data["stripped_escalations"][0]["reason"] == (
            "escalation_evidence_not_in_diff"
        )

    def test_full_replace_semantics(self, tmp_path: Path) -> None:
        diff = _make_diff()
        path = tmp_path / "review-verdict.json"
        path.write_text('{"stale": true}')
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding(severity="NIT"))],
            diff,
            reviewed_sha="sha",
        )
        write_review_verdict(verdict, path)
        data = json.loads(path.read_text())
        assert "stale" not in data

    def test_degraded_reviewer_detail_round_trips_through_persisted_artifact(
        self, tmp_path: Path
    ) -> None:
        # #1775 acceptance criterion: the reason a degraded reviewer states in
        # `detail` must survive synthesis into the persisted record on disk.
        doc = _make_reviewer_doc(
            status="degraded",
            detail="sandbox lacked filesystem access",
            reviewer_role="R1",
        )
        verdict = consolidate_verdict([doc], _make_diff(), reviewed_sha="deadbeef")
        path = tmp_path / "review-verdict.json"
        write_review_verdict(verdict, path)
        data = json.loads(path.read_text())
        assert data["agents_run"][0]["detail"] == "sandbox lacked filesystem access"

    def test_is_terminal_snapshot_defaults_true(self) -> None:
        # #1763: a verdict built by `consolidate_verdict` is a terminal
        # disposition unless a caller (the fix loop) explicitly marks the
        # snapshot it persists as a superseded intermediate.
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding(severity="NIT"))],
            _make_diff(),
            reviewed_sha="sha",
        )
        assert verdict.is_terminal_snapshot is True

    def test_is_terminal_snapshot_round_trips_false(self, tmp_path: Path) -> None:
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding(severity="NIT"))],
            _make_diff(),
            reviewed_sha="sha",
        )
        intermediate = verdict.model_copy(update={"is_terminal_snapshot": False})
        path = tmp_path / "review-verdict.json"
        write_review_verdict(intermediate, path)
        data = json.loads(path.read_text())
        assert data["is_terminal_snapshot"] is False
        assert (
            ReviewVerdict.model_validate_json(path.read_text()).is_terminal_snapshot
            is False
        )


class TestExecutorNeutralContract:
    def test_claude_and_codex_shapes_validate_identically(self) -> None:
        diff = _make_diff(extra_text="-context = removed()")
        good_esc = _make_escalation(evidence_quote="def broken():")
        bad_esc = _make_escalation(evidence_quote="ghost")
        # Two documents modeling the same findings from different executors.
        claude_doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", escalation=good_esc),
            _make_finding(
                severity="SHOULD_FIX",
                line_start=10,
                line_end=10,
                evidence="def broken():",
                escalation=bad_esc,
            ),
            reviewer_role="Reviewer",
        )
        codex_doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", escalation=good_esc),
            _make_finding(
                severity="SHOULD_FIX",
                line_start=10,
                line_end=10,
                evidence="def broken():",
                escalation=bad_esc,
            ),
            reviewer_role="Reviewer",
        )
        v1 = consolidate_verdict([claude_doc], diff, reviewed_sha="sha")
        v2 = consolidate_verdict([codex_doc], diff, reviewed_sha="sha")
        assert v1.model_dump() == v2.model_dump()


class TestReviewVerdictSchemaRegistration:
    def test_schema_surfaces_core_and_new_defs(self) -> None:
        schema = ReviewVerdict.model_json_schema()
        assert "blocking" in schema["properties"]
        assert "must_fix" in schema["properties"]
        assert "reviewed_sha" in schema["properties"]
        assert "stripped_escalations" in schema["properties"]
        assert "EscalationMetadata" in schema["$defs"]
        assert "StrippedEscalation" in schema["$defs"]


class TestReviewerRunRecord:
    def test_construct(self) -> None:
        r = ReviewerRunRecord(reviewer_role="R", status="ok", finding_count=3)
        assert r.finding_count == 3

    def test_detail_defaults_to_empty_string(self) -> None:
        # #1775: detail mirrors ReviewerFindingsDocument.detail's own default.
        r = ReviewerRunRecord(reviewer_role="R", status="ok", finding_count=0)
        assert r.detail == ""

    def test_detail_field_accepts_explicit_value(self) -> None:
        r = ReviewerRunRecord(
            reviewer_role="R",
            status="degraded",
            finding_count=0,
            detail="sandbox lacked filesystem access",
        )
        assert r.detail == "sandbox lacked filesystem access"

    def test_audit_metrics_fields_all_default_when_unset(self) -> None:
        # #1710: every new telemetry field is optional, so pre-#1710 bare
        # construction (and consolidate_verdict's own two construction sites)
        # keep working untouched.
        r = ReviewerRunRecord(reviewer_role="R", status="ok", finding_count=3)
        assert r.thread_id is None
        assert r.effective_model is None
        assert r.duration_seconds is None
        assert r.input_tokens is None
        assert r.cached_input_tokens is None
        assert r.output_tokens is None
        assert r.reasoning_tokens is None
        assert r.terminal_event is None
        assert r.tool_call_counts == {}
        assert r.had_command_evidence is False
        assert r.unexpected_tool_attempts == []

    def test_construct_with_explicit_metrics(self) -> None:
        r = ReviewerRunRecord(
            reviewer_role="R",
            status="ok",
            finding_count=0,
            thread_id="thr-1",
            effective_model=None,
            duration_seconds=12.5,
            input_tokens=100,
            cached_input_tokens=80,
            output_tokens=5,
            reasoning_tokens=1,
            terminal_event="turn.completed",
            tool_call_counts={"command_execution": 2},
            had_command_evidence=True,
            unexpected_tool_attempts=["mcp_tool_call"],
        )
        assert r.thread_id == "thr-1"
        assert r.duration_seconds == pytest.approx(12.5)
        assert r.tool_call_counts == {"command_execution": 2}
        assert r.had_command_evidence is True
        assert r.unexpected_tool_attempts == ["mcp_tool_call"]

    def test_metrics_defaults_are_not_shared_between_instances(self) -> None:
        # Mutable defaults must come from a factory, not a shared literal.
        a = ReviewerRunRecord(reviewer_role="A", status="ok", finding_count=0)
        b = ReviewerRunRecord(reviewer_role="B", status="ok", finding_count=0)
        a.tool_call_counts["x"] = 1
        a.unexpected_tool_attempts.append("y")
        assert b.tool_call_counts == {}
        assert b.unexpected_tool_attempts == []


class TestConsolidateVerdictMetricsByRole:
    """#1710: per-role codex audit metrics land on ReviewerRunRecord."""

    def test_metrics_attach_to_matching_document_record(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(), reviewer_role="Reviewer A")
        verdict = consolidate_verdict(
            [doc],
            diff,
            reviewed_sha="sha",
            metrics_by_role={
                "Reviewer A": {
                    "thread_id": "thr-a",
                    "duration_seconds": 3.5,
                    "input_tokens": 42,
                    "terminal_event": "turn.completed",
                    "tool_call_counts": {"agent_message": 1},
                    "had_command_evidence": True,
                }
            },
        )
        record = verdict.agents_run[0]
        assert record.thread_id == "thr-a"
        assert record.duration_seconds == pytest.approx(3.5)
        assert record.input_tokens == 42
        assert record.terminal_event == "turn.completed"
        assert record.tool_call_counts == {"agent_message": 1}
        assert record.had_command_evidence is True

    def test_role_absent_from_metrics_gets_defaults(self) -> None:
        diff = _make_diff()
        doc_a = _make_reviewer_doc(_make_finding(), reviewer_role="Reviewer A")
        doc_b = _make_reviewer_doc(_make_finding(), reviewer_role="Reviewer B")
        verdict = consolidate_verdict(
            [doc_a, doc_b],
            diff,
            reviewed_sha="sha",
            metrics_by_role={"Reviewer A": {"thread_id": "thr-a"}},
        )
        by_role = {r.reviewer_role: r for r in verdict.agents_run}
        assert by_role["Reviewer A"].thread_id == "thr-a"
        assert by_role["Reviewer B"].thread_id is None
        assert by_role["Reviewer B"].tool_call_counts == {}

    def test_failed_reviewer_record_picks_up_its_metrics(self) -> None:
        # A role that invoked codex and failed still has audit telemetry —
        # the ticket's "runtime failures before the final document" framing.
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(), reviewer_role="Reviewer A")
        verdict = consolidate_verdict(
            [doc],
            diff,
            reviewed_sha="sha",
            failed_reviewers=[
                ReviewerRunFailure(role="Perf Reviewer", reason="timeout")
            ],
            metrics_by_role={
                "Perf Reviewer": {
                    "thread_id": "thr-perf",
                    "terminal_event": "turn.failed",
                    "duration_seconds": 900.0,
                }
            },
        )
        failed = next(r for r in verdict.agents_run if r.status == "failed")
        assert failed.reviewer_role == "Perf Reviewer"
        assert failed.thread_id == "thr-perf"
        assert failed.terminal_event == "turn.failed"
        assert failed.duration_seconds == pytest.approx(900.0)

    def test_none_default_is_byte_identical_to_omitting_the_param(self) -> None:
        # Regression guard for the additive-default claim: the new parameter
        # must not perturb any pre-#1710 verdict.
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="MUST_FIX"))
        without = consolidate_verdict([doc], diff, reviewed_sha="sha")
        with_none = consolidate_verdict(
            [doc], diff, reviewed_sha="sha", metrics_by_role=None
        )
        assert without.model_dump() == with_none.model_dump()

    def test_metrics_never_affect_blocking_or_must_fix(self) -> None:
        # R2/R4 (#1710): metrics are purely observational.
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="MUST_FIX"))
        baseline = consolidate_verdict([doc], diff, reviewed_sha="sha")
        with_metrics = consolidate_verdict(
            [doc],
            diff,
            reviewed_sha="sha",
            metrics_by_role={
                doc.reviewer_role: {
                    "terminal_event": None,
                    "unexpected_tool_attempts": ["mcp_tool_call"],
                    "had_command_evidence": False,
                }
            },
        )
        assert with_metrics.blocking == baseline.blocking
        assert with_metrics.must_fix == baseline.must_fix
        assert with_metrics.review.model_dump() == baseline.review.model_dump()


class TestCapturedDiffStructure:
    def test_file_diffs_and_line_text_round_trip(self) -> None:
        # R6 (#1236): the restructured CapturedDiff carries per-file hunk text
        # and per-file {line: content}, and both survive a JSON round-trip
        # (int line-number keys coerce back from their JSON string form).
        diff = _make_diff("def broken():", files={"src/cw/foo.py": [10]})
        assert diff.file_line_text["src/cw/foo.py"][10] == "def broken():"
        assert "def broken():" in diff.file_diffs["src/cw/foo.py"]
        reloaded = CapturedDiff.model_validate_json(diff.model_dump_json())
        assert reloaded.file_line_text == diff.file_line_text
        assert reloaded.file_diffs == diff.file_diffs
        # #1738: file_window_text (the hunk-context superset of
        # file_line_text) round-trips the same way.
        assert reloaded.file_window_text == diff.file_window_text

    def test_files_matches_file_line_text_keys(self) -> None:
        # The _make_diff invariant the production _capture_diff also upholds:
        # files[f] == sorted(file_line_text[f]).
        diff = _make_diff("a = 1", "b = 2", files={"src/cw/foo.py": [10, 11]})
        for path, line_nums in diff.files.items():
            assert line_nums == sorted(diff.file_line_text[path])


class TestReviewVerdictSchemaVersion:
    def test_schema_version_defaults_to_one(self) -> None:
        diff = _make_diff()
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding())], diff, reviewed_sha="sha"
        )
        assert verdict.schema_version == 1

    def test_schema_version_round_trips(self) -> None:
        diff = _make_diff()
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding())], diff, reviewed_sha="sha"
        )
        reloaded = ReviewVerdict.model_validate_json(verdict.model_dump_json())
        assert reloaded.schema_version == 1

    def test_schema_version_rejects_other_value(self) -> None:
        with pytest.raises(ValidationError):
            ReviewVerdict.model_validate(
                {
                    "schema_version": 2,
                    "blocking": False,
                    "must_fix": [],
                    "reviewed_sha": "sha",
                    "review": derive_review_counts([]).model_dump(),
                }
            )


class TestDebtSeverityAndFields:
    """#1837: the non-blocking DEBT severity and the two admission fields."""

    def test_debt_is_a_valid_severity(self) -> None:
        assert "DEBT" in _VALID_SEVERITIES

    def test_debt_finding_round_trips_as_accepted(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="DEBT"))
        accepted, rejected, stripped = validate_reviewer_document(doc, diff)
        assert len(accepted) == 1
        assert accepted[0].severity == "DEBT"
        assert rejected == []
        assert stripped == []

    def test_debt_deferred_excluded_from_every_aggregate(self) -> None:
        findings = [
            AcceptedFinding(
                finding=_make_finding(severity="DEBT"),
                reviewers=["a"],
                disposition="deferred",
            ),
        ]
        review = derive_review_counts(findings)
        assert review.deferred == 0
        assert review.must_fix_initial == 0
        assert review.should_fix == 0

    def test_debt_alone_does_not_block(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="DEBT"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        assert verdict.blocking is False
        assert verdict.must_fix == []

    def test_transitive_impact_evidence_defaults_blank(self) -> None:
        assert _make_finding().transitive_impact_evidence == ""
        finding = _make_finding(transitive_impact_evidence="def changed(x, y):")
        assert finding.transitive_impact_evidence == "def changed(x, y):"

    def test_release_critical_exception_defaults_blank(self) -> None:
        assert _make_finding().release_critical_exception == ""
        finding = _make_finding(release_critical_exception="unauthenticated write")
        assert finding.release_critical_exception == "unauthenticated write"


class TestVerdictDebtFields:
    def test_debt_and_previous_reviewed_sha_default(self) -> None:
        diff = _make_diff()
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding())], diff, reviewed_sha="sha"
        )
        assert verdict.debt == []
        assert verdict.previous_reviewed_sha is None

    def test_debt_round_trips_through_json(self) -> None:
        diff = _make_diff()
        verdict = consolidate_verdict(
            [_make_reviewer_doc(_make_finding())], diff, reviewed_sha="sha"
        )
        stamped = verdict.model_copy(
            update={"debt": [_make_debt_record()], "previous_reviewed_sha": "old"}
        )
        reloaded = ReviewVerdict.model_validate_json(stamped.model_dump_json())
        assert reloaded.previous_reviewed_sha == "old"
        assert reloaded.debt[0].fingerprint == ("src/cw/foo.py", "bug here")


# -- #2007: content-based re-anchoring for drifted line citations ----------

_RESCUE_LOG = "rescued finding via content-based re-anchoring"
_PERSIST_RESCUE_LOG = "rescued finding's persisted anchor via content-based"

# A real unified diff whose hunk starts at new-file line 100, so every line it
# carries sits far outside `_LINE_ANCHOR_TOLERANCE` of a small declared anchor.
# The marker text below appears ONLY as an unchanged context line: it lands in
# `file_window_text` (which context lines populate) and never in
# `file_line_text` (added lines only) — the exact divergence `_make_diff`
# cannot produce, and the whole point of the persist-path substrate test.
_CONTEXT_ONLY_DIFF = (
    "diff --git a/src/pkg/mod.py b/src/pkg/mod.py\n"
    "--- a/src/pkg/mod.py\n"
    "+++ b/src/pkg/mod.py\n"
    "@@ -100,5 +100,6 @@\n"
    " context_only_marker_alpha\n"  # 100 (context)
    " second_context_line\n"  # 101 (context)
    "+freshly_added_line\n"  # 102 (added)
    " third_context_line\n"  # 103 (context)
    " fourth_context_line\n"  # 104 (context)
)


class TestContentRescueAnchor:
    """Direct unit tests of ``_content_rescue_anchor`` (#2007).

    Pure function of ``(candidates, evidence)`` — no ``CapturedDiff``, no
    ``Finding``, no declared-line hint — so these follow
    ``test_reconcile_evidence_window_direct_*``'s hand-written-dict style.
    """

    def test_evidence_far_beyond_tolerance_is_located(self) -> None:
        assert _content_rescue_anchor(
            {500: "the evidence text"}, "the evidence text"
        ) == (
            500,
            500,
        )

    def test_multiline_evidence_located_across_drifted_window(self) -> None:
        candidates = {900: "alpha line", 901: "beta line", 902: "gamma line"}
        assert _content_rescue_anchor(candidates, "alpha line\nbeta line") == (900, 901)

    def test_evidence_absent_from_file_returns_none(self) -> None:
        candidates = {10: "something real", 11: "also real"}
        assert _content_rescue_anchor(candidates, "fabricated content") is None

    def test_empty_candidates_returns_none(self) -> None:
        assert _content_rescue_anchor({}, "anything") is None

    def test_content_rescue_normalizes_unicode_punctuation(self) -> None:
        # #1976 regression guard: the rescue inherits the shared normalization
        # via _reconcile_evidence_window rather than reimplementing matching.
        candidates = {77: 'label = "quoted"'}
        assert _content_rescue_anchor(candidates, "label = “quoted”") == (
            77,
            77,
        )

    def test_content_rescue_strips_diff_markers(self) -> None:
        candidates = {88: "+def broken():"}
        assert _content_rescue_anchor(candidates, "def broken():") == (88, 88)

    def test_content_rescue_never_synthesizes_a_gap(self) -> None:
        # #1714's false-accept floor: line 11 is genuinely absent from the
        # diff, so a two-line evidence spanning 10-12 must NOT match by
        # silently closing the hole.
        candidates = {10: "first half", 12: "second half"}
        assert _content_rescue_anchor(candidates, "first half\nsecond half") is None


class TestLineExceedsFileLength:
    """Direct unit tests of ``_line_exceeds_file_length`` (#2007)."""

    def test_line_within_real_file_length_returns_false(self, tmp_path: Path) -> None:
        _write_enclosing_def_source(tmp_path, "one\ntwo\nthree\n")
        assert _line_exceeds_file_length(tmp_path, "src/pkg/mod.py", 3) is False

    def test_line_beyond_real_file_length_returns_true(self, tmp_path: Path) -> None:
        _write_enclosing_def_source(tmp_path, "one\ntwo\nthree\n")
        assert _line_exceeds_file_length(tmp_path, "src/pkg/mod.py", 4) is True

    def test_unreadable_file_returns_false(self, tmp_path: Path) -> None:
        # Mirrors _anchor_in_enclosing_def's OSError/UnicodeDecodeError
        # degrade: "can't tell" must never manufacture a rejection reason.
        assert _line_exceeds_file_length(tmp_path, "src/pkg/absent.py", 999) is False


class TestEvidenceRemovedInFixDiff:
    """Direct unit tests of ``_evidence_removed_in_fix_diff`` (#2007).

    The fix-substantiation rescue's predicate: it must recognize a genuinely
    REMOVED line and nothing else, since a false accept here reports a fix as
    substantiated when the cited code was never actually removed.
    """

    def test_removed_line_evidence_is_detected(self) -> None:
        file_diffs = {
            "f.py": "+++ b/f.py\n@@ -1,3 +1,2 @@\n untouched\n-old broken code\n more"
        }
        assert _evidence_removed_in_fix_diff(file_diffs, "f.py", "old broken code")

    def test_multiline_removed_evidence_is_detected(self) -> None:
        file_diffs = {
            "f.py": "+++ b/f.py\n@@ @@\n ctx\n-first gone\n-second gone\n ctx2"
        }
        assert _evidence_removed_in_fix_diff(
            file_diffs, "f.py", "first gone\nsecond gone"
        )

    def test_context_only_evidence_is_not_detected(self) -> None:
        # The MUST-have regression: a plain substring match against the whole
        # hunk text cannot tell "this code was deleted" from "this code is
        # untouched context elsewhere in the same file" — and the second is a
        # false accept in exactly the direction the substantiation bar exists
        # to prevent.
        file_diffs = {
            "f.py": "+++ b/f.py\n@@ @@\n still here untouched\n-unrelated removal\n ctx"
        }
        assert not _evidence_removed_in_fix_diff(
            file_diffs, "f.py", "still here untouched"
        )

    def test_added_line_evidence_is_not_detected(self) -> None:
        file_diffs = {"f.py": "+++ b/f.py\n@@ @@\n ctx\n+brand new line\n ctx2"}
        assert not _evidence_removed_in_fix_diff(file_diffs, "f.py", "brand new line")

    def test_evidence_absent_returns_false(self) -> None:
        file_diffs = {"f.py": "+++ b/f.py\n@@ @@\n ctx\n-something else\n ctx2"}
        assert not _evidence_removed_in_fix_diff(file_diffs, "f.py", "never present")

    def test_unknown_file_returns_false(self) -> None:
        assert not _evidence_removed_in_fix_diff({}, "f.py", "anything")

    def test_normalizes_unicode_punctuation_and_diff_markers(self) -> None:
        file_diffs = {"f.py": '+++ b/f.py\n@@ @@\n ctx\n-label = "quoted"\n ctx2'}
        assert _evidence_removed_in_fix_diff(file_diffs, "f.py", "label = “quoted”")


class TestContentBasedReanchoring:
    """``validate_reviewer_document`` end-to-end over the #2007 rescue."""

    def test_line_anchor_miss_with_evidence_present_far_away_is_accepted(self) -> None:
        diff = _make_diff("drifted target line", files={"src/cw/foo.py": [500]})
        finding = _make_finding(
            line_start=10, line_end=10, evidence="drifted target line"
        )
        accepted, rejected, _stripped = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert rejected == []
        assert len(accepted) == 1

    def test_line_anchor_miss_with_absent_evidence_stays_rejected(self) -> None:
        # #1714's false-accept floor survives the rescue: content genuinely
        # absent from the diff is still rejected at the anchor gate.
        diff = _make_diff(files={"src/cw/foo.py": [10]})
        finding = _make_finding(
            line_start=999, line_end=999, evidence="fabricated nonexistent code"
        )
        _accepted, rejected, _stripped = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert [r.reason for r in rejected] == ["invalid_line_reference"]

    def test_out_of_range_citation_with_worktree_gets_distinct_reason(
        self, tmp_path: Path
    ) -> None:
        _write_enclosing_def_source(tmp_path, "one\ntwo\nthree\n")
        diff = _make_diff("real added line", files={"src/pkg/mod.py": [2]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=999,
            line_end=999,
            evidence="fabricated nonexistent code",
        )
        _accepted, rejected, _stripped = validate_reviewer_document(
            _make_reviewer_doc(finding), diff, worktree=tmp_path
        )
        assert [r.reason for r in rejected] == ["line_reference_out_of_range"]
        assert "3" in rejected[0].detail
        assert rejected[0].detail.strip()

    def test_out_of_range_citation_without_worktree_falls_back_to_generic(self) -> None:
        diff = _make_diff("real added line", files={"src/pkg/mod.py": [2]})
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=999,
            line_end=999,
            evidence="fabricated nonexistent code",
        )
        _accepted, rejected, _stripped = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert [r.reason for r in rejected] == ["invalid_line_reference"]
        assert rejected[0].detail == ""

    def test_content_rescue_logs_info_with_reanchored_location(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        diff = _make_diff("drifted target line", files={"src/cw/foo.py": [500]})
        finding = _make_finding(
            line_start=10, line_end=10, evidence="drifted target line"
        )
        with caplog.at_level(logging.INFO, logger="cw.review_findings"):
            validate_reviewer_document(_make_reviewer_doc(finding), diff)
        assert _RESCUE_LOG in caplog.text
        assert "file=src/cw/foo.py" in caplog.text
        assert "line=500" in caplog.text

    def test_near_miss_within_tolerance_unaffected_by_rescue_path(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Regression: an existing ±3 near-miss resolves at the anchor gate and
        # must never even reach the rescue.
        diff = _make_diff(files={"src/cw/foo.py": [10]})
        finding = _make_finding(line_start=12, line_end=12)
        with caplog.at_level(logging.INFO, logger="cw.review_findings"):
            accepted, rejected, _stripped = validate_reviewer_document(
                _make_reviewer_doc(finding), diff
            )
        assert rejected == []
        assert len(accepted) == 1
        assert _RESCUE_LOG not in caplog.text
        assert _PERSIST_RESCUE_LOG not in caplog.text

    def test_evidence_present_in_different_file_not_matched(self) -> None:
        # Per-file substrate lookup, not the rescue itself, is what scopes the
        # search: evidence real in b.py must not rescue a claim against a.py.
        diff = _make_diff(
            "alpha content",
            "beta content",
            files={"src/cw/a.py": [10], "src/cw/b.py": [20]},
        )
        finding = _make_finding(
            file="src/cw/a.py", line_start=100, line_end=100, evidence="beta content"
        )
        _accepted, rejected, _stripped = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert [r.reason for r in rejected] == ["invalid_line_reference"]

    def test_earlier_hunk_shifting_a_later_finding_is_rescued(self) -> None:
        # The shape the ticket's transcript scan actually found: an earlier
        # hunk grew, pushing a later finding's true line 7 lines past its
        # cited anchor — beyond ±3 — with the evidence genuinely present.
        diff = _make_diff(
            "first added line",
            "shifted target line",
            files={"src/cw/foo.py": [10, 47]},
        )
        finding = _make_finding(
            line_start=40, line_end=40, evidence="shifted target line"
        )
        accepted, rejected, _stripped = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert rejected == []
        assert len(accepted) == 1

    def test_rescued_finding_persists_true_location_not_stale_declared_line(
        self,
    ) -> None:
        diff = _make_diff("drifted target line", files={"src/cw/foo.py": [500]})
        finding = _make_finding(
            line_start=10, line_end=10, evidence="drifted target line"
        )
        accepted, _rejected, _stripped = validate_reviewer_document(
            _make_reviewer_doc(finding), diff
        )
        assert accepted[0].line_start == 500
        assert accepted[0].line_end == 500

    def test_persist_path_content_rescue_never_snaps_onto_context_only_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The persist-path substrate invariant (#1738, carried into #2007):
        # the classify-path rescue searches `file_window_text` (context lines
        # included) and correctly accepts, but the persisted anchor is
        # resolved against `file_line_text` (added lines only) and must NOT
        # land on the context-only line the classify path matched.
        diff = _captured_diff_from_text(_CONTEXT_ONLY_DIFF)
        finding = _make_finding(
            file="src/pkg/mod.py",
            line_start=10,
            line_end=10,
            evidence="context_only_marker_alpha",
        )
        with caplog.at_level(logging.INFO, logger="cw.review_findings"):
            accepted, rejected, _stripped = validate_reviewer_document(
                _make_reviewer_doc(finding), diff
            )
        assert rejected == []
        assert len(accepted) == 1
        assert accepted[0].line_start != 100
        assert _PERSIST_RESCUE_LOG not in caplog.text
