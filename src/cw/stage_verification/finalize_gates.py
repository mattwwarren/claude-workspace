"""cw-run guards, fences and gates for the codex FINALIZE stage (#3062).

Each step calls the same CLI or script the finalize doc's Bash fence calls,
by subprocess (R2): ``check_must_fix_override.py``, ``cw review
check-approval`` (Step 4.0), ``cw review converge config`` and ``verify-tip``
(Step 4.0b). The private adapters behind those commands are never imported.
Parks are built from the shared ``dev_queue/gates.py`` constants with the
doc's ``next_actions``.

:func:`run_finalize_quality_gates` is stage-neutral: it serves the reuse path
(the doc's Step 4c.6) and the create path, where it replaces ``/prep-pr``
Step 7, and runs before any push or PR create. It never autofixes and never
commits; after a red gate it only reads ``git diff`` to say so when the gate
rewrote tracked files (#3769).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from cw._git import LOCAL_GIT_TIMEOUT_SECONDS, git_clean_env, git_output
from cw.auto_dev_result.schema._vocab import EXTERNAL_STATE_BLOCKER_REASON
from cw.codex_fix_loop.gates import (
    GateFailure,
    GatePlan,
    GateScope,
    plan_cycle_gates,
    run_cycle_gates,
)
from cw.codex_fix_loop.guard_script import find_guard_script, script_marker_ok
from cw.codex_review import CODEX_FIX_DIRTY_START
from cw.dev_queue.gates import APPROVAL_PARK_FRICTION_PREFIX, STALE_TIP_FRICTION_PREFIX
from cw.plan_files import resolve_review_scope_tier
from cw.stage_verification.baseline import DirtyStart, capture_cycle_baseline
from cw.stage_verification.finalize_ci_evidence import (
    CiContext,
    DeferredGate,
    _HoldReleaseError,
    awaiting_ci_evidence_block,
    consult_create,
    consult_reuse,
)
from cw.stage_verification.finalize_delta_confirm import (
    confirm_resolution,
    delta_eligible,
)
from cw.stage_verification.finalize_failure_identity import (
    failure_identity,
    identity_matches,
    is_recognised_gate,
)
from cw.stage_verification.finalize_result import (
    AGENT_BLOCK,
    CODEX_MUST_FIX_FINDINGS,
    OPERATOR_UNAVAILABLE,
    REQUEUE_STAGE_FINALIZE,
    REQUEUE_STAGE_REVIEW,
    SCOPE_TIER_UNRESOLVABLE,
    STAGE4_MUST_FIX_OVERRIDE,
    FinalizeBlock,
    FinalizeFacts,
    finalize_approval_park,
)
from cw.stage_verification.impl_feedback import fetch_default_branch
from cw.stage_verification.impl_type_gate import gate_worktree
from cw.stage_verification.posted_text import redact_and_cap

if TYPE_CHECKING:
    from cw.auto_dev_result import ScopeTier
    from cw.local_runner import GitFacts
    from cw.models import TicketTask

OVERRIDE_SCRIPT_NAME: Final = "check_must_fix_override.py"
MIN_OVERRIDE_SCRIPT_VERSION: Final = 1
OVERRIDE_ABSENT_FRICTION: Final = "check_must_fix_override: script absent, skipped"
OVERRIDE_FRICTION: Final = "must_fix_operator_override"

APPROVAL_NOT_REQUIRED: Final = "REVIEW_APPROVAL_NOT_REQUIRED"
APPROVAL_RECORDED: Final = "REVIEW_APPROVAL_RECORDED"
_APPROVAL_PARK_LINE: Final = "REVIEW_APPROVAL_PARK:"
APPROVAL_CHECK_PREFIX: Final = "review_approval_check: "
TIP_CHECK_PREFIX: Final = "review_tip_check: "
TIP_FETCH_FAILED: Final = "tip_check_fetch_failed"
CLEAN_CONVERGENCE: Final = "clean"

#: Bounded helper timeouts (seconds) for the cw CLI probes: helper clocks,
#: never stage timers (ADR-0014). A clock-expired probe is retryable.
CW_CLI_TIMEOUT: Final = 300
_SCRIPT_TIMEOUT: Final = 120
_EXIT_BLOCKED: Final = 1
_EXIT_MISSING: Final = frozenset({127, 2})
_TAIL: Final = 500
#: Most tracked paths the red-gate rewrite note names; the rest are counted.
REWRITE_NOTE_MAX_PATHS: Final = 5
_DIFF_TIMEOUT: Final = 30
_CYCLE: Final = 0
_CONTINUE_TOKENS: Final = frozenset({APPROVAL_NOT_REQUIRED, APPROVAL_RECORDED})

#: The semantic auto-resolve's gate path (#3542): no CI consult, no classifier.
RESOLVE_PATH: Final = "resolve"
#: The base-branch-state classifier's scratch tree prefix and leftover token.
BASE_WORKTREE_PREFIX: Final = "cw-gate-wt-finalize-"
BASE_WORKTREE_LEFTOVER: Final = "finalize_base_worktree_leftover"
#: ``sh`` could not execute (126) or find (127) the command.
_SHELL_COULD_NOT_RUN: Final = frozenset({126, 127})
#: ``sh`` reports a signal-killed child as 128 + the signal number.
_SIGNAL_EXIT_FLOOR: Final = 128

_log = logging.getLogger(__name__)


class _DidNotComplete:
    """Marker: a base gate run the shell or the runner could not complete."""


_DID_NOT_COMPLETE: Final = _DidNotComplete()

_PARK_TEXT: Final = (
    " — the large-tier review approval is not valid for this head, and "
    "`cw dev-queue approve` is refused at the terminal stage on a lane with a "
    "review stage. The operator releases this park with `cw dev-queue requeue "
    "{ticket} -c {client} --stage review`, which returns the ticket to review "
    "to obtain the large-tier approval; review parks it for approval and `cw "
    "dev-queue approve {ticket} -c {client}` then records that approval at "
    "review. On a lane whose pipeline has no review stage that requeue is "
    "refused: `cw dev-queue show {ticket} -c {client}` lists the release that "
    "lane accepts. For unresolved_tier, first fix the tier source (a `**Scope "
    "tier:** small|large` line in .cw/plan.md, or scope_hint). Do not re-run "
    "finalize to release this park: with no approval recorded this check "
    "parks it again"
)

_REWRITE_NOTE: Final = (
    "The gate rewrote tracked files, which are left uncommitted in the "
    "worktree: {paths}. Next step: commit the formatting on the branch, then "
    "requeue REVIEW with `cw dev-queue requeue {ticket} -c {client} --stage "
    "review` (clean mode needs a re-review of the new tip)."
)


def _tail(text: str) -> str:
    return redact_and_cap(text.strip()[-_TAIL:])


def run_cw_cli(
    worktree: Path, args: list[str], *, timeout: float = CW_CLI_TIMEOUT
) -> subprocess.CompletedProcess[str] | None:
    """Run ``python -m cw <args>`` in *worktree*; ``None`` when it could not finish.

    ``sys.executable -m cw`` avoids a ``PATH`` dependence on ``cw``.
    """
    try:
        return subprocess.run(
            [sys.executable, "-m", "cw", *args],
            cwd=worktree,
            env=git_clean_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


# -- tier, branch ------------------------------------------------------------


def resolve_tier(
    task: TicketTask, plan_text: str | None, facts: GitFacts
) -> ScopeTier | FinalizeBlock:
    """The resolved ``scope.tier``, or the ``scope_tier_unresolvable`` block."""
    # No fork point means the diff stat is not measurable: pass None so the
    # resolver can return None and cw blocks instead of guessing a tier from 0/0.
    measured = (facts["files"], facts["lines_actual"]) if facts["fork_point"] else None
    tier = resolve_review_scope_tier(plan_text, task.scope_hint, measured)
    if tier is not None:
        return tier
    return FinalizeBlock(
        SCOPE_TIER_UNRESOLVABLE,
        "no scope tier source resolved (no `**Scope tier:**` line in "
        ".cw/plan.md, no scope_hint, and an unmeasurable diff); cw never "
        "guesses a tier. Fix the tier source, then requeue finalize.",
        retry_eligible=True,
    )


def check_branch(worktree: Path, expected: str) -> FinalizeBlock | None:
    """Block unless the checked-out branch is *expected* (never a detached HEAD).

    ``push_and_verify_head`` pushes ``HEAD`` to the checked-out branch, while
    the Detector Guard resolves the branch from the row: the two must agree
    before anything looks up or pushes a branch.
    """
    try:
        current = git_output(["branch", "--show-current"], cwd=worktree).strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        current = ""
        why = f" (git failed: {_tail(str(exc))})"
    else:
        why = ""
    if current == expected:
        return None
    shown = repr(current) if current else "a detached HEAD"
    return FinalizeBlock(
        EXTERNAL_STATE_BLOCKER_REASON,
        f"the worktree has {shown} checked out{why}, but this row's branch is "
        f"{expected!r}; cw pushes and looks up PRs only for the row's branch. "
        "Check out the row's branch before requeueing; nothing was pushed.",
    )


# -- must-fix override ---------------------------------------------------------


@dataclass(frozen=True)
class OverrideOutcome:
    """The must-fix override check: a block, or its friction and override JSON."""

    block: FinalizeBlock | None = None
    friction: list[str] = field(default_factory=list)
    override_json: str | None = None


def _status_of(stdout: str) -> str | None:
    try:
        data = json.loads(stdout)
    except (ValueError, RecursionError):
        return None
    status = data.get("status") if isinstance(data, dict) else None
    return status if isinstance(status, str) else None


def run_must_fix_override(worktree: Path, head: str) -> OverrideOutcome:
    """Run ``check_must_fix_override.py`` against the review verdict and *head*."""
    script = find_guard_script(worktree, OVERRIDE_SCRIPT_NAME)
    if script is None:
        return OverrideOutcome(friction=[OVERRIDE_ABSENT_FRICTION])
    if not script_marker_ok(script, MIN_OVERRIDE_SCRIPT_VERSION):
        return OverrideOutcome(
            block=FinalizeBlock(
                AGENT_BLOCK,
                "MUST_FIX Override Verification: HEADLESS BLOCK — "
                f"{OVERRIDE_SCRIPT_NAME} at {script} — missing/stale "
                f"cw-script-version marker (need >= {MIN_OVERRIDE_SCRIPT_VERSION})",
            )
        )
    argv = [
        sys.executable,
        str(script),
        "--verdict",
        str(worktree / ".claude" / "review-verdict.json"),
        "--context",
        str(worktree / ".claude" / "cw-context.json"),
        "--head",
        head,
    ]
    try:
        done = subprocess.run(
            argv,
            cwd=worktree,
            env=git_clean_env(),
            capture_output=True,
            text=True,
            timeout=_SCRIPT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return OverrideOutcome(
            block=FinalizeBlock(
                OPERATOR_UNAVAILABLE,
                f"{OVERRIDE_SCRIPT_NAME} did not finish: {_tail(str(exc))}",
                retry_eligible=True,
            )
        )
    return _override_outcome(done)


def _override_outcome(done: subprocess.CompletedProcess[str]) -> OverrideOutcome:
    output = done.stdout.strip() or done.stderr.strip()
    if done.returncode == 0:
        if _status_of(done.stdout) == "overridden":
            return OverrideOutcome(
                friction=[OVERRIDE_FRICTION], override_json=done.stdout.strip()
            )
        return OverrideOutcome()
    if done.returncode == _EXIT_BLOCKED:
        return OverrideOutcome(
            block=FinalizeBlock(
                CODEX_MUST_FIX_FINDINGS,
                f"{_tail(output)}\n\nThe operator either fixes the findings or "
                "runs `cw dev-queue approve <ticket> --override-must-fix --reason "
                '"..."` and then `cw dev-queue requeue <ticket> --stage finalize`.',
                blocker_stage=STAGE4_MUST_FIX_OVERRIDE,
            )
        )
    return OverrideOutcome(
        block=FinalizeBlock(
            AGENT_BLOCK,
            f"{OVERRIDE_SCRIPT_NAME} exited {done.returncode} (a guard/tooling "
            f"failure, not a MUST_FIX disposition): {_tail(output)}",
        )
    )


# -- Step 4.0 / 4.0b -----------------------------------------------------------


def _approval_token(tier: str, worktree: Path) -> str:
    """Step 4.0's one-line verdict, the doc's fence rules applied."""
    if tier == "small":
        return APPROVAL_NOT_REQUIRED
    done = run_cw_cli(worktree, ["review", "check-approval", "--tier", tier])
    if done is None:
        return (
            f"{_APPROVAL_PARK_LINE} unreadable — cw review check-approval did not "
            "finish (timed out or could not start)"
        )
    if done.returncode in _EXIT_MISSING:
        return (
            f"{_APPROVAL_PARK_LINE} unreadable — cw is missing or older than this "
            f"command (exit {done.returncode}); upgrade cw or put it on PATH"
        )
    line = done.stdout.strip().splitlines()[0] if done.stdout.strip() else ""
    if done.returncode != 0 or not line:
        return (
            f"{_APPROVAL_PARK_LINE} unreadable — no verdict (cw review "
            f"check-approval exited {done.returncode} with no output)"
        )
    return line


def _park_word(line: str) -> str:
    rest = line.removeprefix(_APPROVAL_PARK_LINE).strip()
    word = rest.split(" ", 1)[0] if rest else ""
    return word or "unreadable"


@dataclass(frozen=True)
class FenceOutcome:
    """The Step 4.0/4.0b fence: a park payload, or the friction a pass logs."""

    park: dict[str, object] | None = None
    friction: list[str] = field(default_factory=list)


def _tip_stale_token(
    done: subprocess.CompletedProcess[str] | None, delta: str | None = None
) -> tuple[str, str]:
    """``(friction token, reason)`` for a failed ``verify-tip``.

    *delta* is the delta-confirm round's park outcome, appended inside the
    token's parentheses (``stale_tip_reason`` matches the whole entry).
    """
    try:
        data = json.loads(done.stdout) if done is not None else None
    except (ValueError, RecursionError):
        data = None
    reason = data.get("reason") if isinstance(data, dict) else None
    detail = data.get("detail") if isinstance(data, dict) else None
    if not isinstance(reason, str) or not reason:
        return STALE_TIP_FRICTION_PREFIX, ""
    suffix = f"; delta confirm: {delta}" if delta is not None else ""
    if isinstance(detail, str) and detail:
        return f"{STALE_TIP_FRICTION_PREFIX} ({reason}: {detail}{suffix})", reason
    return f"{STALE_TIP_FRICTION_PREFIX} ({reason}{suffix})", reason


def _convergence(worktree: Path) -> str | None:
    """``converge config``'s ``convergence``; ``None`` is the error rule (stale)."""
    done = run_cw_cli(
        worktree, ["review", "converge", "config", "--loop-backend", "codex"]
    )
    if done is None or done.returncode != 0:
        return None
    try:
        data = json.loads(done.stdout)
    except (ValueError, RecursionError):
        return None
    value = data.get("convergence") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def _tip_check(
    worktree: Path,
    facts: FinalizeFacts,
    tier: ScopeTier,
    check_line: str,
    *,
    ticket_id: str,
    client_name: str,
) -> FenceOutcome:
    """Step 4.0b (clean mode only); ``blocking`` skips it.

    A tip refused only for merge-resolution commits since the clean round
    (``delta_confirm_eligible``) gets one delta-confirm round first (#3542).
    """
    convergence = _convergence(worktree)
    if convergence is not None and convergence != CLEAN_CONVERGENCE:
        return FenceOutcome()
    done = None
    delta: str | None = None
    if convergence is not None:
        done = run_cw_cli(worktree, ["review", "converge", "verify-tip"])
        if done is not None and done.returncode == 0:
            return FenceOutcome(friction=[f"{TIP_CHECK_PREFIX}accepted"])
        if done is not None and done.returncode == 1 and delta_eligible(done.stdout):
            delta = confirm_resolution(
                worktree,
                ticket_id=ticket_id,
                client_name=client_name,
                run_cli=run_cw_cli,
            )
            if delta is None:
                return FenceOutcome(friction=[f"{TIP_CHECK_PREFIX}accepted"])
    token, reason = _tip_stale_token(done, delta)
    next_action = (
        REQUEUE_STAGE_FINALIZE if reason == TIP_FETCH_FAILED else REQUEUE_STAGE_REVIEW
    )
    park = finalize_approval_park(
        facts, tier=tier, friction=[check_line, token], next_action=next_action
    )
    return FenceOutcome(park=park)


def run_approval_fence(
    worktree: Path,
    facts: FinalizeFacts,
    *,
    tier: ScopeTier,
    ticket_id: str,
    client_name: str,
) -> FenceOutcome:
    """Step 4.0 then, in clean mode, Step 4.0b: the HEAD-sensitive fence.

    Run before any PR create or reuse, and again immediately before the arm.
    """
    line = _approval_token(tier, worktree)
    if line in _CONTINUE_TOKENS:
        check_line = f"{APPROVAL_CHECK_PREFIX}{line}"
        outcome = _tip_check(
            worktree,
            facts,
            tier,
            check_line,
            ticket_id=ticket_id,
            client_name=client_name,
        )
        if outcome.park is not None:
            return outcome
        return FenceOutcome(friction=[check_line, *outcome.friction])
    word = _park_word(line)
    park_line = f"{APPROVAL_PARK_FRICTION_PREFIX} {word}" + _PARK_TEXT.format(
        ticket=ticket_id, client=client_name
    )
    park = finalize_approval_park(
        facts,
        tier="large",
        friction=[park_line, f"{APPROVAL_CHECK_PREFIX}{word}"],
    )
    return FenceOutcome(park=park)


# -- quality gates -------------------------------------------------------------


def _no_autofix(plan: GatePlan) -> GatePlan:
    return replace(
        plan, gates=tuple(replace(gate, autofix=None) for gate in plan.gates)
    )


@dataclass(frozen=True)
class FinalizeGateRun:
    """The quality-gate step: a block, the friction it logs, the gates CI owns.

    *deferred* is non-empty only on the create path, when the base branch maps
    detected gates to CI checks; the caller parks once the PR exists.
    """

    block: FinalizeBlock | None = None
    friction: list[str] = field(default_factory=list)
    deferred: tuple[DeferredGate, ...] = ()
    failed_gate: str = ""
    failed_output: str = ""


@dataclass(frozen=True)
class _CiPlan:
    """The gate plan after the CI-evidence consult, or the park it ended in."""

    plan: GatePlan
    friction: list[str] = field(default_factory=list)
    deferred: tuple[DeferredGate, ...] = ()
    block: FinalizeBlock | None = None


def _without(plan: GatePlan, names: frozenset[str]) -> GatePlan:
    return replace(
        plan, gates=tuple(gate for gate in plan.gates if gate.name not in names)
    )


def _consult(plan: GatePlan, worktree: Path, path: str, ci: CiContext) -> _CiPlan:
    """Drop the gates CI owns: deferred (create) or green-covered (reuse).

    Nothing to consult for an unavailable plan, one with no gates, or the
    ``resolve`` path (the semantic auto-resolve's merge). A reuse park is
    returned as the block; no gate runs.
    """
    if (
        path == RESOLVE_PATH
        or plan.script is None
        or plan.why_unavailable is not None
        or not plan.gates
    ):
        return _CiPlan(plan)
    if path == "create":
        try:
            deferred, friction = consult_create(plan.script, worktree, ci.base)
        except _HoldReleaseError as exc:
            return _CiPlan(
                plan,
                block=awaiting_ci_evidence_block("hold_error", ci, cause=exc.cause),
            )
        gone = frozenset(d.gate for d in deferred)
        return _CiPlan(_without(plan, gone), friction, deferred)
    reuse = consult_reuse(plan.script, worktree, ci)
    if reuse.park is not None:
        block = awaiting_ci_evidence_block(reuse.park.flavor, ci, park=reuse.park)
        return _CiPlan(plan, block=block)
    return _CiPlan(_without(plan, reuse.skip_names), list(reuse.friction))


def _nothing_to_run(
    label: str, detected: GatePlan, ci_plan: _CiPlan
) -> FinalizeGateRun:
    """No gate is left to run: none detected, all deferred, or all CI-covered."""
    if not detected.gates:
        tail = [f"{label}none_detected"]
    elif ci_plan.deferred:
        tail = []
    else:
        tail = [f"{label}passed"]
    return FinalizeGateRun(None, [*ci_plan.friction, *tail], ci_plan.deferred)


def _failure_block(path: str, failure: GateFailure) -> FinalizeBlock:
    if failure.kind == "unavailable":
        return FinalizeBlock(
            EXTERNAL_STATE_BLOCKER_REASON,
            "the repo's quality gates could not be run: "
            + redact_and_cap(failure.why or "no reason recorded"),
        )
    what = (
        f"failed with exit code {failure.exit_code}"
        if failure.kind == "red"
        else "passed but changed files (the gate dirtied the worktree)"
    )
    nothing = (
        "Nothing was pushed."
        if path == RESOLVE_PATH
        else "Nothing was pushed, created or armed."
    )
    details = (
        f"the branch tip failed the FINALIZE {path}-path quality gate "
        f"`{failure.gate}`: it {what}. {nothing}"
    )
    if failure.output:
        details += "\n\nLast lines of gate output:\n" + redact_and_cap(failure.output)
    return FinalizeBlock(AGENT_BLOCK, details)


def _rewritten_tracked_paths(worktree: Path, ignored: frozenset[str]) -> list[str]:
    """Tracked files modified in *worktree* against ``HEAD``, minus *ignored*.

    Read-only (``git diff``, never an add). ``--diff-filter=M`` keeps untracked
    scratch out: ``run_cycle_gates`` already staged every untracked file as
    Added after the gate ran, so an unfiltered diff would list it. Best-effort:
    a failed read yields ``[]``.
    """
    try:
        out = git_output(
            ["diff", "--name-only", "--no-renames", "--diff-filter=M", "HEAD"],
            cwd=worktree,
            timeout=_DIFF_TIMEOUT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        # Best-effort: a failed read must never mask the gate's own failure.
        return []
    return sorted({line for line in out.splitlines() if line and line not in ignored})


def _path_list(paths: list[str]) -> str:
    """Backticked, redacted names of up to ``REWRITE_NOTE_MAX_PATHS`` paths."""
    shown = paths[:REWRITE_NOTE_MAX_PATHS]
    text = ", ".join(f"`{redact_and_cap(p)}`" for p in shown)
    more = len(paths) - len(shown)
    if more:
        text += f" and {more} more"
    return text


def _with_rewrite_note(
    block: FinalizeBlock, worktree: Path, scope: GateScope, ignored: frozenset[str]
) -> FinalizeBlock:
    """*block* with a note naming the tracked files the red gate rewrote."""
    paths = _rewritten_tracked_paths(worktree, ignored)
    if not paths:
        return block
    note = _REWRITE_NOTE.format(
        paths=_path_list(paths), ticket=scope.ticket_id, client=scope.client
    )
    return replace(block, details=f"{block.details}\n\n{note}")


def run_finalize_quality_gates(
    worktree: Path,
    *,
    scope: GateScope,
    path: str,
    ci: CiContext,
    tolerated: frozenset[str] = frozenset(),
) -> FinalizeGateRun:
    """Run the repo's quality gates on the branch tip; never fix, never commit.

    *path* is ``"reuse"``, ``"create"`` or ``"resolve"`` and names the friction
    literal (``reuse_path_gates: passed|none_detected`` is the doc's). A red
    gate or a dirtied tree is ``agent_block``; an unavailable detector
    ``external_state_block``; a dirty start ``codex_fix_dirty_start``. On the
    create and reuse paths a red gate that fails the same way on a clean
    ``origin/<base>`` is ``external_state_block`` (:func:`reproduces_on_base`).
    The ``resolve`` path (the semantic auto-resolve's merge, #3542) has no CI
    consult, no rewrite note and no base classifier.

    Gates the base branch maps to CI (*ci*) do not run here: the create path
    defers them (returned in ``deferred``), the reuse path skips the ones
    already green at the PR head and parks, as the returned block, on a
    pending, missing or red mapped check. A consult that cannot answer runs
    every gate locally.

    A red gate that left tracked files modified adds a bounded note naming them
    to the block's details, read-only and best-effort (#3769).
    """
    label = f"{path}_path_gates: "
    baseline = capture_cycle_baseline(worktree, tolerated=tolerated)
    if isinstance(baseline, DirtyStart):
        return FinalizeGateRun(
            FinalizeBlock(
                CODEX_FIX_DIRTY_START,
                "the worktree was not clean before the FINALIZE quality gates "
                "(uncommitted or unfinished work); nothing was pushed or created",
            )
        )
    detected = _no_autofix(plan_cycle_gates(worktree, scope))
    ci_plan = _consult(detected, worktree, path, ci)
    if ci_plan.block is not None:
        return FinalizeGateRun(ci_plan.block, ci_plan.friction)
    if detected.why_unavailable is None and not ci_plan.plan.gates:
        return _nothing_to_run(label, detected, ci_plan)
    failure = run_cycle_gates(
        ci_plan.plan,
        worktree=worktree,
        review_artifacts=baseline.review_artifacts,
        scope=scope,
        tokens=[],
        touched=frozenset(),
    )
    if failure is None:
        return FinalizeGateRun(
            None, [*ci_plan.friction, f"{label}passed"], ci_plan.deferred
        )
    block = _red_block(
        worktree, failure, path=path, ci_plan=ci_plan, scope=scope, ci=ci
    )
    if block is None:
        block = _failure_block(path, failure)
        if failure.kind == "red" and path != RESOLVE_PATH:
            block = _with_rewrite_note(
                block, worktree, scope, baseline.review_artifacts
            )
    return FinalizeGateRun(
        block,
        ci_plan.friction,
        failed_gate=failure.gate,
        failed_output=redact_and_cap(failure.output) if failure.output else "",
    )


def _red_block(
    worktree: Path,
    failure: GateFailure,
    *,
    path: str,
    ci_plan: _CiPlan,
    scope: GateScope,
    ci: CiContext,
) -> FinalizeBlock | None:
    """The ``external_state_block`` for a red gate base shows too, else ``None``."""
    if failure.kind != "red" or path == RESOLVE_PATH:
        return None
    sha = reproduces_on_base(
        worktree, failure, plan=ci_plan.plan, scope=scope, base_branch=ci.base
    )
    if sha is None:
        return None
    return _external_block(path, failure, base_branch=ci.base, sha=sha)


# -- base-branch-state classifier (#2320, #3542) -------------------------------


def has_tracked_changes(worktree: Path) -> bool:
    """Whether *worktree* has any tracked modification (staged or not).

    ``git status --porcelain --untracked-files=no``, tolerated paths and review
    artifacts included. A failed read counts as changes (fail closed).
    """
    try:
        out = git_output(
            ["status", "--porcelain", "--untracked-files=no"],
            cwd=worktree,
            timeout=LOCAL_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return True
    return bool(out.strip())


def _inside(value: str, root: Path) -> bool:
    try:
        return Path(value).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def _environment_in_worktree(worktree: Path) -> bool:
    """A Python environment the base run could import branch code through."""
    if os.environ.get("UV_PROJECT_ENVIRONMENT"):
        return True
    root = worktree.resolve()
    venv = os.environ.get("VIRTUAL_ENV")
    if venv and _inside(venv, root):
        return True
    entries = os.environ.get("PATH", "").split(os.pathsep)
    return any(entry and _inside(entry, root) for entry in entries)


def _unclassified(scope: GateScope, gate: str, reason: str) -> None:
    _log.warning(
        "finalize_base_state_unclassified ticket=%s session=%s gate=%s reason=%s",
        scope.ticket_id,
        scope.session_id,
        gate,
        reason,
    )


def _not_comparable(worktree: Path, failure: GateFailure) -> str | None:
    """Why the branch failure cannot be compared with a clean base, before a fetch."""
    if failure.exit_code in _SHELL_COULD_NOT_RUN:
        return "exit_code_not_comparable"
    if has_tracked_changes(worktree):
        return "tracked_changes"
    if _environment_in_worktree(worktree):
        return "environment_in_worktree"
    if (
        failure_identity(failure.gate, failure.full_output, roots=(str(worktree),))
        is None
    ):
        return "no_identity"
    return None


def _incomplete(result: GateFailure | None) -> bool:
    """A base run the shell or the runner could not complete."""
    if result is None:
        return False
    if result.kind == "unavailable":
        return True
    code = result.exit_code
    return (
        result.kind == "red"
        and code is not None
        and (code in _SHELL_COULD_NOT_RUN or code < 0 or code > _SIGNAL_EXIT_FLOOR)
    )


def _base_run(
    plan: GatePlan, scratch: Path, scope: GateScope
) -> GateFailure | None | _DidNotComplete:
    """Run the one-gate *plan* in the scratch tree; never autofix, never park."""
    try:
        result = run_cycle_gates(
            plan,
            worktree=scratch,
            review_artifacts=frozenset(),
            scope=scope,
            tokens=[],
            touched=frozenset(),
        )
    except (subprocess.SubprocessError, OSError):
        return _DID_NOT_COMPLETE
    return _DID_NOT_COMPLETE if _incomplete(result) else result


def _reset_scratch(scratch: Path, sha: str) -> bool:
    """``git reset --hard <sha>`` in the scratch tree only; False on failure."""
    try:
        git_output(
            ["reset", "--hard", sha], cwd=scratch, timeout=LOCAL_GIT_TIMEOUT_SECONDS
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return True


def _compared_run(
    plan: GatePlan, scratch: Path, scope: GateScope, *, warm: bool, sha: str
) -> GateFailure | None | str:
    """The base run that is compared, or the ``unclassified`` reason.

    An unrecognised gate (*warm*) runs twice: the warm-up only provisions the
    tree, a tracked-only reset clears its leftovers, and the second run alone
    is compared.
    """
    if warm:
        if isinstance(_base_run(plan, scratch, scope), _DidNotComplete):
            return "did_not_complete"
        if not _reset_scratch(scratch, sha):
            return "reset_failed"
    result = _base_run(plan, scratch, scope)
    return "did_not_complete" if isinstance(result, _DidNotComplete) else result


def _verdict(
    failure: GateFailure, base: GateFailure | None, *, worktree: Path, scratch: Path
) -> str | None:
    """``None`` when *base* shows *failure*, else the ``unclassified`` reason."""
    if base is None or base.kind != "red":
        return "base_passed"
    if base.exit_code != failure.exit_code:
        return "exit_code_differs"
    mine = failure_identity(failure.gate, failure.full_output, roots=(str(worktree),))
    theirs = failure_identity(base.gate, base.full_output, roots=(str(scratch),))
    if mine is None or theirs is None:
        return "no_identity"
    return None if identity_matches(mine, theirs) else "identity_differs"


def _classify_in_scratch(
    worktree: Path, failure: GateFailure, *, plan: GatePlan, scope: GateScope, sha: str
) -> str | None:
    """Run the failed gate on a scratch tree at *sha*; ``None`` when reproduced."""
    one = replace(plan, gates=tuple(g for g in plan.gates if g.name == failure.gate))
    try:
        with gate_worktree(
            worktree,
            sha,
            root=str(worktree.parent),
            ticket=scope.ticket_id,
            prefix=BASE_WORKTREE_PREFIX,
            leftover=BASE_WORKTREE_LEFTOVER,
        ) as scratch:
            base = _compared_run(
                one,
                scratch,
                scope,
                warm=not is_recognised_gate(failure.gate),
                sha=sha,
            )
            if isinstance(base, str):
                return base
            return _verdict(failure, base, worktree=worktree, scratch=scratch)
    except (subprocess.SubprocessError, OSError):
        return "scratch_tree_failed"


def _base_sha(worktree: Path, base_branch: str) -> str | None:
    try:
        return git_output(
            ["rev-parse", "--verify", f"origin/{base_branch}^{{commit}}"],
            cwd=worktree,
            timeout=LOCAL_GIT_TIMEOUT_SECONDS,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None


def reproduces_on_base(
    worktree: Path,
    failure: GateFailure,
    *,
    plan: GatePlan,
    scope: GateScope,
    base_branch: str,
) -> str | None:
    """The ``origin/<base>`` sha when a red gate fails the same way there.

    The #2320 base-branch-state classifier. It runs only the failed gate, pinned
    from *plan* and with the branch run's own *scope*, in a detached scratch
    worktree beside *worktree*: the session worktree is never checked out,
    reset or staged. A recognised gate (``pytest*``, ``mypy*``, ``ruff*``) runs
    once there; any other gate runs twice and only the second run is compared.
    Reproduced means the compared run completed red with the same exit code
    and every branch failure in it (``finalize_failure_identity``). Anything
    else is ``None`` (``unclassified``, which stays ``agent_block``) and logs one
    WARNING ``finalize_base_state_unclassified`` naming the reason. A gate run
    is never killed (ADR-0014); only the fetch, ``rev-parse`` and reset are
    bounded.
    """
    reason = _not_comparable(worktree, failure)
    sha: str | None = None
    if reason is None and fetch_default_branch(worktree, base_branch) is not None:
        reason = "fetch_failed"
    if reason is None:
        sha = _base_sha(worktree, base_branch)
        if sha is None:
            reason = "scratch_tree_failed"
    if reason is None and sha is not None:
        reason = _classify_in_scratch(
            worktree, failure, plan=plan, scope=scope, sha=sha
        )
    if reason is not None:
        _unclassified(scope, failure.gate, reason)
        return None
    return sha


def _external_block(
    path: str, failure: GateFailure, *, base_branch: str, sha: str
) -> FinalizeBlock:
    """The reproduced-on-base park: the cause is outside this branch."""
    gate = failure.gate
    details = (
        f"the FINALIZE {path}-path quality gate `{gate}` fails with exit code "
        f"{failure.exit_code} on this branch and, with the same failure, on a "
        f"clean origin/{base_branch} ({sha}); the cause is outside this branch. "
        f"Nothing was pushed, created or armed. To clear it, `{gate}` must pass "
        f"on origin/{base_branch}; then merge origin/{base_branch} into the "
        "branch, push, and requeue finalize."
    )
    if failure.output:
        details += "\n\nLast lines of gate output:\n" + redact_and_cap(failure.output)
    return FinalizeBlock(EXTERNAL_STATE_BLOCKER_REASON, details, retry_eligible=False)


def gate_scope(ticket_id: str, client: str, session_id: str) -> GateScope:
    """The gate log labels for one FINALIZE run (cycle 0)."""
    return GateScope(
        ticket_id=ticket_id, client=client, session_id=session_id, cycle=_CYCLE
    )
