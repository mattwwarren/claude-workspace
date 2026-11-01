"""Clean-mode REVIEW prompt text for the opencode lane (#2806).

Under ``review.convergence: clean`` the opencode REVIEW worker drives the
converge-to-clean ledger itself, from the prompt, with the same deterministic
``cw review converge ...`` primitives the Claude-native prose uses, stamped
``--loop-backend opencode``. opencode runs fire-and-forget with no cw wrapper
process (``cw.opencode_runner.RealOpencodeRunner.launch``), so there is no
Python driver to hang a gate on: ``converge finalize`` / ``verify-tip`` is the
mechanical fail-closed backstop against a skipped step, and the independent
confirm round (``cw review confirm --backend <ledger confirm backend>``)
cross-checks the agent's own loop round.

This is a text-only module: :mod:`cw.opencode_runner` stays the composer
(it owns the stage preamble and the sentinel rules) and imports
:func:`clean_review_body` and :data:`CLEAN_REVIEW_SENTINEL_TEMPLATE` from here.
Nothing here imports :mod:`cw.opencode_runner`; the only cw imports are the
:data:`cw.models.OPENCODE_BACKEND` and :data:`cw.models.HOOK_CONTEXT_RELATIVE_PATH`
constants, so the loop backend the prompt stamps and the hook-context file it
reads cannot drift from the ones the engine and the context writer use. The
blocking REVIEW prompt is a
separate builder there and never reads this module.
"""

from __future__ import annotations

from cw.models import HOOK_CONTEXT_RELATIVE_PATH, OPENCODE_BACKEND

#: Shell ceiling on the foreground ``cw review confirm`` call, in seconds. The
#: same 600 s ceiling the Claude-native prose uses: a harness limit, not a
#: cw-imposed budget (ADR-0014). An overrun parks ``agent_block``.
CONFIRM_TIMEOUT_SECONDS = 600
#: The matching explicit tool timeout for the Bash call that runs it.
CONFIRM_TOOL_TIMEOUT_MS = CONFIRM_TIMEOUT_SECONDS * 1000

#: ``jq`` filter turning the exit-0 stdout of ``cw review confirm`` into a
#: ``record-round`` confirm payload. The verdict is passed raw; the prompt pipes
#: it through ``record-round --defer-should-fix``, which decides it.
CONFIRM_RECORD_JQ = (
    '{kind:"confirm",backend:.backend,reviewed_sha:.reviewed_sha,'
    "roster:[.verdict.agents_run[].reviewer_role],verdict:.verdict}"
)

_CONTEXT_FILE = HOOK_CONTEXT_RELATIVE_PATH.as_posix()
_CLIENT_FROM_CONTEXT = f"$(jq -r .client {_CONTEXT_FILE})"
_PUSH = "`git push origin HEAD:refs/heads/<branch-name>`"


def _guard_steps(ticket_id: str) -> str:
    return (
        "This client runs the converge-to-clean review "
        "(review.convergence: clean). The review is recorded in a "
        "convergence ledger by `cw review converge` commands; finalize "
        "refuses a ticket whose ledger has not converged, so every step "
        "below is mandatory and in order. Every exit is blocked with "
        'blocker.stage "stage3_review" unless Step 6 says otherwise. Use '
        "`jq` to read JSON; a missing `jq` is an agent_block exit.\n"
        "0. Guard: run `cw review converge config --loop-backend "
        f"{OPENCODE_BACKEND}`. "
        'Continue only if it exits 0 and prints "convergence": "clean" and '
        '"dispatch_refusal": null. Anything else (a non-zero exit, '
        '"blocking", a non-null dispatch_refusal) → emit blocked with '
        'blocker.reason "agent_block", retry_eligible false and the output '
        "quoted in blocker.details. Never fall back to a blocking-mode "
        "review.\n"
        "1. Determine the default branch and record "
        "FORK_POINT=$(git merge-base origin/<default-branch> HEAD). If "
        "`git diff --numstat $FORK_POINT..HEAD` shows 0 files, emit status "
        'empty_diff_blocked with blocker.reason "empty_diff_no_commits" — a '
        "clean verdict over an empty branch reviewed nothing.\n"
        f"2. Ledger: run `cw review converge init --ticket-id {ticket_id} "
        '--fork-point "$FORK_POINT" --reviewed-sha "$(git rev-parse HEAD)" '
        f"--loop-backend {OPENCODE_BACKEND}`. If its output has `released`, append "
        "`convergence_stop_released: <released.reason> via "
        "<released.cause>` to friction_highlights; if `reset_reason` is "
        "non-null, append `convergence_ledger_reset: <reset_reason>`. Then "
        "run `cw review converge status` and obey its `next_action`:\n"
        "   - stopped → append `convergence_stop_reemitted: <stop.reason>` "
        "and take the exit for `stop.reason` in the exit table below; review "
        "nothing and record no round.\n"
        "   - confirm → Step 4.\n"
        "   - done → Step 5.\n"
        "   - loop_round or fix → Step 3 (a pending fix is reviewed again as "
        "a fresh loop round).\n"
        "   A non-zero exit from init or status → agent_block with its "
        "stderr quoted.\n"
    )


def _loop_round_step() -> str:
    return (
        "3. Loop round — review, record, THEN fix:\n"
        "   a. Run `cw review converge diff-base >.cw/diff-base.json; echo "
        '"DIFF_BASE_RC=$?"` and read DIFF_BASE=$(jq -r .diff_base '
        ".cw/diff-base.json). A non-zero DIFF_BASE_RC → agent_block. Use "
        "$DIFF_BASE for every diff in this round.\n"
        "   b. Read .cw/plan.md if present. Review the ENTIRE diff "
        "`git diff $DIFF_BASE..HEAD` adversarially: correctness, plan "
        "conformance, missing or weak tests, unhandled error paths, "
        "security. Run the repo's quality gates (read CLAUDE.md for the "
        "list).\n"
        "   c. Build FINDINGS, a JSON list with one object per MUST_FIX or "
        'SHOULD_FIX finding: {"severity": "MUST_FIX" or "SHOULD_FIX", '
        '"file": "<repo-relative path>", "summary": "<one line>", '
        '"outcome": "<outcome>", "rationale": "<why>"}. Outcome: a MUST_FIX '
        'you will fix → "fix"; a MUST_FIX that is a false positive → '
        '"reject" with the rationale; every SHOULD_FIX → "defer" with the '
        "rationale (SHOULD_FIX never gates); a MUST_FIX with no diff anchor "
        "that only the operator can discharge (a ticket to file, an external "
        'system) → "operator_action" with the action as the rationale. Omit '
        "NIT findings. Reuse the same file and summary wording when a "
        "finding recurs, so the ledger recognizes it. The MUST_FIX and "
        "SHOULD_FIX counts of the first loop round you record are "
        "review.must_fix_initial and review.should_fix in the sentinel.\n"
        "   d. Record the round BEFORE fixing anything. Write the payload to a "
        "file through a QUOTED-delimiter heredoc, so a quote or `$` inside a "
        "finding summary is never interpreted by the shell, then record it by "
        "path. The next four lines are shell text: run them exactly as shown, "
        "from the start of the line, because the heredoc ends only at a line "
        "that is exactly JSON (an indented terminator never ends it, and the "
        "record-round line below would be swallowed into the payload file):\n"
        "cat > .cw/loop-round.json <<'JSON'\n"
        '{"kind":"loop","backend":"' + OPENCODE_BACKEND + '",'
        '"reviewed_sha":"<git rev-parse HEAD>","roster":["' + OPENCODE_BACKEND + '"],'
        '"findings":<FINDINGS>}\n'
        "JSON\n"
        "cw review converge record-round .cw/loop-round.json\n"
        "      Then obey its `decision`:\n"
        '      - continue → fix every "fix" finding, re-run tests and '
        f"gates, commit, {_PUSH}, then run Step 3 again at the new HEAD.\n"
        "      - confirm_required → Step 4.\n"
        "      - clean → Step 5 (the only path when the confirm backend is "
        "none: the ledger converges on this loop round alone).\n"
        "      - stop_not_converging, stop_max_cycles, stop_operator_action, "
        "stop_confirm_unavailable → the exit table below.\n"
        "      A non-zero exit recorded nothing: agent_block with stderr "
        "quoted and `convergence_record_round_failed: exit=<n>` in "
        "friction_highlights. Never retry it and never continue as clean.\n"
    )


def _confirm_call(ticket_id: str) -> str:
    return (
        '      CONFIRM_BACKEND="$(cw review converge status | jq -r '
        '.sentinel_review.confirm_backend)"\n'
        '      echo "CONFIRM_BACKEND=$CONFIRM_BACKEND"\n'
        '      case "$CONFIRM_BACKEND" in\n'
        "        claude|codex)\n"
        "          cw review converge diff-base >.cw/diff-base.json "
        "2>.cw/diff-base-stderr.txt\n"
        '          DIFF_BASE_RC=$?; echo "DIFF_BASE_RC=$DIFF_BASE_RC"\n'
        '          if [ "$DIFF_BASE_RC" -eq 0 ]; then\n'
        f"            timeout {CONFIRM_TIMEOUT_SECONDS} cw review confirm "
        f'--ticket-id {ticket_id} -c "{_CLIENT_FROM_CONTEXT}" '
        '--backend "$CONFIRM_BACKEND" >.cw/confirm-stdout.json '
        "2>.cw/confirm-stderr.txt\n"
        '            echo "CONFIRM_RC=$?"\n'
        "          else tail -n 20 .cw/diff-base-stderr.txt; fi ;;\n"
        '        *) echo "CONFIRM_SKIPPED" ;;\n'
        "      esac\n"
    )


def _confirm_failed_call() -> str:
    """The exit-3 block: cause and detail come from the captured files."""
    return (
        "       CAUSE=\"$(jq -r '.cause // empty' .cw/confirm-stdout.json "
        '2>/dev/null)"\n'
        '       [ -n "$CAUSE" ] || CAUSE=no_output\n'
        "       DETAIL=\"$(jq -r '.detail // empty' .cw/confirm-stdout.json "
        '2>/dev/null)"\n'
        '       [ -n "$DETAIL" ] || DETAIL="$(tail -n 20 '
        '.cw/confirm-stderr.txt)"\n'
        '       cw review converge confirm-failed --cause "$CAUSE" '
        f'--detail "$DETAIL" -c "{_CLIENT_FROM_CONTEXT}"\n'
    )


def _confirm_step(ticket_id: str) -> str:
    return (
        "4. Confirm round — an independent reviewer, no fix loop. The "
        "confirm backend is the one the ledger stamped (the only one "
        "record-round accepts). Run this as ONE foreground Bash call with "
        f"the tool timeout parameter set explicitly to "
        f"{CONFIRM_TOOL_TIMEOUT_MS} (milliseconds); never run it in the "
        "background and never accept a background continuation:\n"
        + _confirm_call(ticket_id)
        + "   Branch on what it printed:\n"
        "   - CONFIRM_SKIPPED (CONFIRM_BACKEND is none, empty or null; a "
        "healthy ledger never gets here, because none converges at Step 3 "
        'without a confirm round) → agent_block, details "confirm step '
        "reached with confirm backend '<CONFIRM_BACKEND>'\". Do NOT run "
        "cw review confirm.\n"
        "   - DIFF_BASE_RC non-zero → agent_block quoting the tail of "
        ".cw/diff-base-stderr.txt, friction `confirm_invocation_error: "
        "exit=<DIFF_BASE_RC>`.\n"
        "   - CONFIRM_RC=0: a verdict, NOT a clean signal. Decide on "
        ".result_blocker_reason in .cw/confirm-stdout.json BEFORE recording:\n"
        "     - null, codex_must_fix_findings, scope_tier_unresolvable or "
        "empty_diff_no_commits → record it:\n"
        f"       jq '{CONFIRM_RECORD_JQ}' .cw/confirm-stdout.json | cw review "
        "converge record-round - --defer-should-fix\n"
        "       and obey `decision`: clean → Step 5; continue → read "
        ".verdict.accepted in .cw/confirm-stdout.json, fix every MUST_FIX, "
        f"re-run tests and gates, commit, {_PUSH}, then Step 3 at the new "
        "HEAD; a stop_* decision → the exit table. A non-zero exit → "
        "agent_block with stderr quoted, friction "
        "`convergence_record_round_failed: exit=<n>`.\n"
        "     - codex_must_fix_mechanically_rejected → blocked with "
        'blocker.reason "review_blocked", details quoting '
        ".verdict.rejected_must_fix[].raw; record nothing.\n"
        '     - anything else → agent_block, details "confirm round '
        'returned result_blocker_reason <r>", friction '
        "`confirm_round_failed: <r>`; record nothing.\n"
        "   - CONFIRM_RC=3 (the backend is down): record it with the block "
        "below, which takes the cause and detail from the captured files "
        "(never paste captured text into the command yourself):\n"
        + _confirm_failed_call()
        + "     and obey its `decision`: "
        "stop_confirm_unavailable → blocked with its `blocker` "
        '(reason "confirm_backend_unavailable", retry_eligible true) and its '
        "`review`, friction `confirm_backend_unavailable: <cause>`; "
        "stop_confirm_escalated → blocked with its `blocker` (agent_block) "
        "and its `review`, friction `confirm_backend_escalated: <cause> "
        "x<consecutive>`. Only exit 3 is a backend outage.\n"
        "   - CONFIRM_RC=4 (the round failed on a healthy backend) → "
        'agent_block, details "confirm round failed on a healthy backend: '
        '<.cause>: <.detail>", friction `confirm_round_failed: <.cause>`; '
        "record nothing and run no confirm-failed.\n"
        "   - CONFIRM_RC=2 (a usage error) → agent_block quoting "
        ".cw/confirm-stderr.txt, friction `confirm_invocation_error: "
        "exit=2`.\n"
        "   - CONFIRM_RC=124, or no CONFIRM_RC because the tool timeout ended "
        "the call (an overrun, NOT an outage) → agent_block, details "
        f'"foreground ceiling {CONFIRM_TIMEOUT_SECONDS}s exceeded; not '
        "backgrounded (headless). Last captured output: <tail of "
        '.cw/confirm-stdout.json, else of .cw/confirm-stderr.txt>", friction '
        "`confirm_invocation_error: exit=timeout`. Never "
        "confirm_backend_unavailable.\n"
        '   - any other CONFIRM_RC → agent_block, details "cw review confirm '
        'exited <n>: <stderr tail>", friction `confirm_invocation_error: '
        "exit=<n>`.\n"
    )


def _finalize_steps() -> str:
    return (
        f"5. Finalize: push first ({_PUSH}), then run `cw review converge "
        "finalize` and obey its `decision`:\n"
        "   - accepted (exit 0) → copy its `sentinel_review` object into "
        "review (convergence_mode, rounds_run, converged, "
        "final_round_clean_sha, confirm_backend, fix_cycles_used) and its "
        "`friction_lines` into friction_highlights; set review.reviewed_sha "
        "to final_round_clean_sha. Under confirm backend none these read "
        'confirm_backend "none" and rounds_run 1: copy them as printed.\n'
        "   - confirm_required, or rereview_required with reason "
        "commits_after_clean_round or no_clean_round → Step 3 at HEAD.\n"
        "   - rereview_required with reason head_not_origin_tip → push "
        "again and re-run finalize once; still not accepted → agent_block "
        "quoting its output.\n"
        "   - any other non-zero exit → agent_block with stderr quoted.\n"
        "6. Resolve scope.tier: the `**Scope tier:**` line in .cw/plan.md "
        "if present, else derive from the diff (small = no forbidden-area "
        "touches; large otherwise). Exit status, only after Step 5 "
        "accepted: tier small → stage_complete; tier large → "
        "review_pending_approval (the orchestrator approves the ship). Set "
        "review.agents_run to 1 — you are the single loop reviewer; never "
        "report a review pass that did not happen.\n"
    )


def _exit_table() -> str:
    return (
        "Exit table (status blocked, review.converged false, the review "
        "fields from `cw review converge status`'s `sentinel_review`, "
        "review.reviewed_sha = git rev-parse HEAD; rounds_run null when no "
        "round is recorded yet, since the schema rejects 0). For a stop, quote "
        "`stop.release_hint` and `stop.settle_payload` from `cw review "
        "converge status` in blocker.details:\n"
        "- stop_not_converging (stop.reason oscillation or no_progress) → "
        'blocker.reason "review_not_converging", retry_eligible false.\n'
        "- stop_max_cycles (stop.reason max_cycles) → blocker.reason "
        '"review_blocked", details naming the max_cycles cap and '
        "fix_cycles_used.\n"
        "- stop_operator_action (stop.reason operator_action) → "
        'blocker.reason "review_operator_actionable", details listing each '
        "operator action.\n"
        "- stop_confirm_unavailable → blocker.reason "
        '"confirm_backend_unavailable", retry_eligible true (the '
        "confirm-failed blocker).\n"
        '- every other failure named above → blocker.reason "agent_block".\n'
    )


def clean_review_body(ticket_id: str) -> str:
    """The clean-mode REVIEW instructions for *ticket_id* (Steps 0-6 and exits).

    The caller wraps it with the stage preamble before and the sentinel rules
    and :data:`CLEAN_REVIEW_SENTINEL_TEMPLATE` after.
    """
    return (
        _guard_steps(ticket_id)
        + _loop_round_step()
        + _confirm_step(ticket_id)
        + _finalize_steps()
        + _exit_table()
    )


CLEAN_REVIEW_SENTINEL_TEMPLATE = """
Sentinel template (stage3_review, clean mode — scope.tier, scope.lines_actual,
and health.lowest_agent_confidence are REQUIRED non-null; review.converged is
true only after Step 5 accepted, and then review.reviewed_sha equals
review.final_round_clean_sha):
<<<AUTO_DEV_RESULT
{
  "schema_version": 5,
  "ticket_id": "<ticket-id>",
  "status": "<stage_complete | review_pending_approval |
             empty_diff_blocked | blocked>",
  "stage_reached": "stage3_review",
  "scope": {"tier": "<small|large>", "files": <changed files>,
            "lines_estimate": 0,
            "lines_actual": <added+removed vs fork point>,
            "forbidden_touched": false},
  "plan_source": "<github_issue_existing | generated | free_text | none>",
  "branch": "<branch-name>",
  "worktree_path": "<absolute worktree path>",
  "fork_point_sha": "<fork point sha>",
  "commits": ["<pushed fix sha>"],
  "pr": null,
  "review": {"must_fix_initial": <first loop round MUST_FIX count>,
             "should_fix": <first loop round SHOULD_FIX count>,
             "fix_cycles_used": <sentinel_review.fix_cycles_used>,
             "deferred": 0, "agents_run": 1,
             "reviewed_sha": "<final_round_clean_sha, or HEAD when blocked>",
             "convergence_mode": "clean",
             "rounds_run": <sentinel_review.rounds_run>,
             "converged": <sentinel_review.converged>,
             "final_round_clean_sha": <sentinel_review.final_round_clean_sha>,
             "confirm_backend": "<sentinel_review.confirm_backend>"},
  "health": {"lowest_agent_confidence": "<HIGH|MEDIUM|LOW>",
             "any_incomplete_risk": false, "shortcuts": [],
             "recommendation": "<PROCEED|EXIT_FOR_HUMAN_REVIEW>",
             "downgrade_applied": false, "fix_loop_escalated": false},
  "friction_highlights": [],
  "ambiguities": [],
  "blocker": null,
  "prior_pr_warnings": [],
  "next_actions": []
}
AUTO_DEV_RESULT>>>
"""
