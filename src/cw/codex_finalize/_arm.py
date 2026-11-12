"""The auto-merge seam check, the arm and its verification (#3062, R4/R5).

The arm goes through ``prep_pr_finalize.py`` only, resolved and run by the
existing repair-recipe wrappers (``_probe_arm_script``, ``_run_arm``); cw never
calls ``gh pr merge`` itself. Verification reads the PR back with
``fetch_pr_head`` (state OPEN or MERGED, head oid equal to the local HEAD,
``auto_merge_armed`` equal to the claim) and ``fetch_required_failing_checks``
(``None`` is unverifiable CI and blocks; a non-empty list is friction).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from cw._git import git_clean_env
from cw.auto_dev_result.schema._vocab import EXTERNAL_STATE_BLOCKER_REASON
from cw.codex_finalize._result import (
    AUTOMERGE_NOT_ARMED,
    OPERATOR_UNAVAILABLE,
    FinalizeBlock,
)
from cw.codex_fix_loop.posted_text import redact_and_cap
from cw.gh import fetch_pr_head, fetch_required_failing_checks
from cw.reconcile.review_recipes.repair_git import (
    RepairStopError,
    _probe_arm_script,
    _run_arm,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.gh import PrHead

ALLOWED: Final = 0
DISALLOWED: Final = 1
EXIT_ARM_DISALLOWED: Final = 3
FREEZE_PREFIX: Final = "changelog_merge_freeze:"
FAILING_CHECKS_PREFIX: Final = "required_checks_failing_at_emission: "

_CHECK_TIMEOUT: Final = 60
_VERIFIED_STATES: Final = frozenset({"OPEN", "MERGED"})
_TAIL: Final = 500


def _invocation_block(
    code: int | None, tail: str, pr: dict[str, object]
) -> FinalizeBlock:
    return FinalizeBlock(
        AUTOMERGE_NOT_ARMED,
        f"arm-automerge invocation error (exit {code}, not a gh failure): {tail}",
        pr_info=pr,
    )


def check_automerge_allowed(
    worktree: Path, pr_info: dict[str, object]
) -> bool | FinalizeBlock:
    """``prep_pr_finalize.py check-automerge-allowed``: True, False, or a block.

    A missing script or any exit other than 0/1 is an invocation-error
    ``automerge_not_armed`` (not retryable); a timeout is retryable.
    """
    try:
        script = _probe_arm_script(worktree)
    except RepairStopError as exc:
        return _invocation_block(None, exc.detail, pr_info)
    try:
        done = subprocess.run(
            [
                sys.executable,
                str(script),
                "check-automerge-allowed",
                "--repo-path",
                str(worktree),
            ],
            cwd=worktree,
            env=git_clean_env(),
            capture_output=True,
            text=True,
            timeout=_CHECK_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            f"check-automerge-allowed did not finish: {redact_and_cap(str(exc))}",
            retry_eligible=True,
        )
    if done.returncode == ALLOWED:
        return True
    if done.returncode == DISALLOWED:
        return False
    tail = redact_and_cap(done.stderr.strip()[-_TAIL:])
    return _invocation_block(done.returncode, tail, pr_info)


@dataclass(frozen=True)
class ArmResult:
    """What the arm did: whether auto-merge is armed, or the block."""

    armed: bool = False
    block: FinalizeBlock | None = None
    friction: list[str] = field(default_factory=list)


def _refusal(gh_stderr: str, tail: str, pr_info: dict[str, object]) -> FinalizeBlock:
    """Arm exit 1: gh refused (verbatim ``gh_stderr``), retryable."""
    text = gh_stderr or tail
    details = f"arm-automerge failed; gh_stderr: {text}"
    if gh_stderr.startswith(FREEZE_PREFIX) or FREEZE_PREFIX in text:
        details += (
            "\nThe CHANGELOG merge freeze refused the arm: run `cw worktree "
            "changelog-sync` on the branch, push, then requeue finalize."
        )
    return FinalizeBlock(
        AUTOMERGE_NOT_ARMED, details, retry_eligible=True, pr_info=pr_info
    )


def arm(
    worktree: Path, *, pr_number: int, head_sha: str, pr_info: dict[str, object]
) -> ArmResult:
    """Run ``arm-automerge`` pinned to *head_sha* (exit 3 ships unarmed)."""
    try:
        script = _probe_arm_script(worktree)
    except RepairStopError as exc:
        return ArmResult(block=_invocation_block(None, exc.detail, pr_info))
    outcome = _run_arm(script, pr_number, worktree, head_sha)
    if outcome.returncode is None:
        return ArmResult(
            block=FinalizeBlock(
                OPERATOR_UNAVAILABLE,
                f"arm-automerge did not finish: {outcome.stderr_tail}",
                retry_eligible=True,
            )
        )
    if outcome.returncode == 0:
        return ArmResult(armed=True)
    if outcome.returncode == EXIT_ARM_DISALLOWED:
        return ArmResult(armed=False)
    if outcome.returncode == 1:
        return ArmResult(
            block=_refusal(outcome.gh_stderr, outcome.stderr_tail, pr_info)
        )
    return ArmResult(
        block=_invocation_block(outcome.returncode, outcome.stderr_tail, pr_info)
    )


def _head_block(
    head: PrHead,
    *,
    pr_url: str,
    head_sha: str,
    armed_claim: bool,
    pr_info: dict[str, object],
) -> FinalizeBlock | None:
    """The read-back checks on a PR head (state, oid, armed flag)."""
    if head.state not in _VERIFIED_STATES:
        return FinalizeBlock(
            EXTERNAL_STATE_BLOCKER_REASON,
            f"{pr_url} is {head.state} after the arm, not OPEN or MERGED",
            retry_eligible=True,
        )
    if head.oid != head_sha:
        return FinalizeBlock(
            EXTERNAL_STATE_BLOCKER_REASON,
            f"{pr_url}'s head {head.oid} is not the local HEAD {head_sha}",
            retry_eligible=True,
        )
    if head.state == "OPEN" and head.auto_merge_armed is not armed_claim:
        return FinalizeBlock(
            AUTOMERGE_NOT_ARMED,
            f"{pr_url} reads back auto_merge_armed={head.auto_merge_armed}, but "
            f"cw's arm reported {armed_claim}",
            retry_eligible=True,
            pr_info=pr_info,
        )
    return None


def verify_pr_state(
    worktree: Path,
    *,
    pr_url: str,
    head_sha: str,
    armed_claim: bool,
    pr_info: dict[str, object],
) -> tuple[FinalizeBlock | None, list[str]]:
    """The R4 verification; ``(block, friction)``."""
    head = fetch_pr_head(pr_url, cwd=worktree)
    if head is None:
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            f"gh could not read {pr_url} back after the arm",
            retry_eligible=True,
        ), []
    block = _head_block(
        head,
        pr_url=pr_url,
        head_sha=head_sha,
        armed_claim=armed_claim,
        pr_info=pr_info,
    )
    if block is not None:
        return block, []
    checks = fetch_required_failing_checks(pr_url, cwd=worktree)
    if checks is None:
        return FinalizeBlock(
            OPERATOR_UNAVAILABLE,
            f"the required checks of {pr_url} could not be read (CI unverifiable)",
            retry_eligible=True,
        ), []
    if checks:
        names = ", ".join(check.name for check in checks)
        return None, [FAILING_CHECKS_PREFIX + redact_and_cap(names)]
    return None, []
