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
commits.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

from cw._git import git_clean_env, git_output
from cw.auto_dev_result.schema._vocab import EXTERNAL_STATE_BLOCKER_REASON
from cw.codex_finalize._result import (
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
from cw.codex_fix_loop.baseline import DirtyStart, capture_cycle_baseline
from cw.codex_fix_loop.gates import (
    GatePlan,
    GateScope,
    plan_cycle_gates,
    run_cycle_gates,
)
from cw.codex_fix_loop.guard_script import find_guard_script, script_marker_ok
from cw.codex_fix_loop.posted_text import redact_and_cap
from cw.codex_review import CODEX_FIX_DIRTY_START
from cw.dev_queue.gates import APPROVAL_PARK_FRICTION_PREFIX, STALE_TIP_FRICTION_PREFIX
from cw.plan_files import resolve_review_scope_tier

if TYPE_CHECKING:
    from pathlib import Path

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
_CYCLE: Final = 0
_CONTINUE_TOKENS: Final = frozenset({APPROVAL_NOT_REQUIRED, APPROVAL_RECORDED})

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


def _tip_stale_token(done: subprocess.CompletedProcess[str] | None) -> tuple[str, str]:
    """``(friction token, reason)`` for a failed ``verify-tip``."""
    try:
        data = json.loads(done.stdout) if done is not None else None
    except (ValueError, RecursionError):
        data = None
    reason = data.get("reason") if isinstance(data, dict) else None
    detail = data.get("detail") if isinstance(data, dict) else None
    if not isinstance(reason, str) or not reason:
        return STALE_TIP_FRICTION_PREFIX, ""
    if isinstance(detail, str) and detail:
        return f"{STALE_TIP_FRICTION_PREFIX} ({reason}: {detail})", reason
    return f"{STALE_TIP_FRICTION_PREFIX} ({reason})", reason


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
    worktree: Path, facts: FinalizeFacts, tier: ScopeTier, check_line: str
) -> FenceOutcome:
    """Step 4.0b (clean mode only); ``blocking`` skips it."""
    convergence = _convergence(worktree)
    if convergence is not None and convergence != CLEAN_CONVERGENCE:
        return FenceOutcome()
    done = None
    if convergence is not None:
        done = run_cw_cli(worktree, ["review", "converge", "verify-tip"])
        if done is not None and done.returncode == 0:
            return FenceOutcome(friction=[f"{TIP_CHECK_PREFIX}accepted"])
    token, reason = _tip_stale_token(done)
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
        outcome = _tip_check(worktree, facts, tier, check_line)
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


def run_finalize_quality_gates(
    worktree: Path, *, scope: GateScope, path: str
) -> tuple[FinalizeBlock | None, list[str]]:
    """Run the repo's quality gates on the branch tip; never fix, never commit.

    *path* is ``"reuse"`` or ``"create"`` and names the friction literal
    (``reuse_path_gates: passed|none_detected`` is the doc's). A red gate or a
    dirtied tree is ``agent_block``; an unavailable detector
    ``external_state_block``; a dirty start ``codex_fix_dirty_start``.
    """
    label = f"{path}_path_gates: "
    baseline = capture_cycle_baseline(worktree)
    if isinstance(baseline, DirtyStart):
        return FinalizeBlock(
            CODEX_FIX_DIRTY_START,
            "the worktree was not clean before the FINALIZE quality gates "
            "(uncommitted or unfinished work); nothing was pushed or created",
        ), []
    plan = _no_autofix(plan_cycle_gates(worktree, scope))
    if plan.why_unavailable is None and not plan.gates:
        return None, [f"{label}none_detected"]
    failure = run_cycle_gates(
        plan,
        worktree=worktree,
        review_artifacts=baseline.review_artifacts,
        scope=scope,
        tokens=[],
        touched=frozenset(),
    )
    if failure is None:
        return None, [f"{label}passed"]
    if failure.kind == "unavailable":
        return FinalizeBlock(
            EXTERNAL_STATE_BLOCKER_REASON,
            "the repo's quality gates could not be run: "
            + redact_and_cap(failure.why or "no reason recorded"),
        ), []
    what = (
        f"failed with exit code {failure.exit_code}"
        if failure.kind == "red"
        else "passed but changed files (the gate dirtied the worktree)"
    )
    details = (
        f"the branch tip failed the FINALIZE {path}-path quality gate "
        f"`{failure.gate}`: it {what}. Nothing was pushed, created or armed."
    )
    if failure.output:
        details += "\n\nLast lines of gate output:\n" + redact_and_cap(failure.output)
    return FinalizeBlock(AGENT_BLOCK, details), []


def gate_scope(ticket_id: str, client: str, session_id: str) -> GateScope:
    """The gate log labels for one FINALIZE run (cycle 0)."""
    return GateScope(
        ticket_id=ticket_id, client=client, session_id=session_id, cycle=_CYCLE
    )
