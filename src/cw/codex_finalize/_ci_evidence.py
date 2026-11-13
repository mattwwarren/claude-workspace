"""The CI-evidence consult for the codex FINALIZE quality gates (#3618).

When the base branch's committed ``quality_gates.ci_evidence`` maps a detected
gate to CI checks, the create path does not run that gate before the PR exists
(it is deferred and the stage parks ``awaiting_ci_evidence`` once the PR is
created, unarmed) and the reuse path skips it only when every mapped check is
green at the PR head. The decision is made once, by ``prep_pr_state.py``'s
``ci-deferral`` and ``ci-reuse-plan``; this module shells that script and
consumes its JSON, re-implementing no aggregation (R4). It never imports the
script. Any failure to get a usable answer is "unavailable": the gates run
locally and a ``ci_evidence_unavailable:`` friction token says why (fail closed).

Codex never writes a CI-evidence hold (cw decides arming in Python), but a
Claude-created hold on the same worktree would make the shared arm seam ship
unarmed silently, so :func:`release_ci_hold` clears it before a reuse pass arms.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from cw._git import git_clean_env
from cw.codex_finalize._result import AWAITING_CI_EVIDENCE, FinalizeBlock
from cw.codex_fix_loop.posted_text import redact_and_cap
from cw.codex_review import STAGE5_POST_CREATE
from cw.reconcile.review_recipes.repair_git import (
    RepairStopError,
    _probe_arm_script,
)

if TYPE_CHECKING:
    from pathlib import Path

ParkFlavor = Literal["pending", "red"]
BlockFlavor = Literal["deferred", "pending", "red", "hold_error"]

UNAVAILABLE_PREFIX: Final = "ci_evidence_unavailable: "
CREATE_DEFERRED_PREFIX: Final = "create_path_gate_ci_deferred: "
REUSE_COVERED_PREFIX: Final = "reuse_path_gate_ci_covered: "
CI_DEFERRAL_COMMAND: Final = "ci-deferral"
CI_REUSE_PLAN_COMMAND: Final = "ci-reuse-plan"
CI_HOLD_COMMAND: Final = "ci-hold"
CI_HOLD_RELEASE_ACTION: Final = "release"

#: Bounded helper timeout (seconds): a helper clock, never a stage timer
#: (ADR-0014). An expired consult is "unavailable", never a park.
CONSULT_TIMEOUT: Final = 300
_HOLD_MARKER: Final = CI_HOLD_COMMAND
_PARK_FLAVORS: Final = ("pending", "red")
_HOLD_STATES: Final = ("none", "active", "stale", "corrupt")
_TAIL: Final = 500
_NOT_ARMED: Final = "cw did not arm auto-merge"
_NOT_RUN: Final = "did not run the gate locally"


@dataclass(frozen=True)
class CiContext:
    """What the consult knows about the run.

    *pr_number*, *pr_url* are ``None`` on the create path (no PR yet); *head* is
    the local HEAD (the PR head once created).
    """

    base: str
    pr_number: int | None = None
    pr_url: str | None = None
    head: str | None = None


@dataclass(frozen=True)
class DeferredGate:
    """A gate mapped to CI checks: *checks* are its mapped check names."""

    gate: str
    checks: tuple[str, ...]


@dataclass(frozen=True)
class ReusePark:
    """``ci-reuse-plan``'s ``park``: the gate and the check that decided it."""

    flavor: ParkFlavor
    gate: str
    checks: tuple[str, ...]
    failed_check: str
    check_state: str
    failed_url: str | None
    sha: str


@dataclass(frozen=True)
class ReusePlan:
    """The reuse decision: a park, or the gates to skip (and what to log)."""

    park: ReusePark | None = None
    skip_names: frozenset[str] = frozenset()
    friction: tuple[str, ...] = ()


class _UnavailableError(Exception):
    """The helper gave no usable answer; *cause* is the friction suffix."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


class _HoldReleaseError(_UnavailableError):
    """A hold could not be released safely before the create path continued."""


def _unavailable(subcommand: str, cause: str) -> str:
    return f"{UNAVAILABLE_PREFIX}{subcommand} {cause}"


def _launch(
    script: Path, worktree: Path, args: list[str]
) -> subprocess.CompletedProcess[str] | str:
    """Run ``<script> <args>`` in *worktree*, or say why it could not finish."""
    try:
        return subprocess.run(
            [sys.executable, str(script), *args],
            cwd=worktree,
            env=git_clean_env(),
            capture_output=True,
            text=True,
            timeout=CONSULT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "timed out"
    except OSError:
        return "could not start"


def _exit_cause(done: subprocess.CompletedProcess[str]) -> str:
    tail = redact_and_cap(done.stderr.strip()[-_TAIL:])
    return f"exit {done.returncode}: {tail}" if tail else f"exit {done.returncode}"


def _call(script: Path, worktree: Path, args: list[str]) -> dict[str, object]:
    """The helper's JSON object; :class:`_UnavailableError` when it is not usable."""
    done = _launch(script, worktree, args)
    if isinstance(done, str):
        raise _UnavailableError(done)
    if done.returncode != 0:
        raise _UnavailableError(_exit_cause(done))
    try:
        data = json.loads(done.stdout)
    except (ValueError, RecursionError):
        msg = "output is not JSON"
        raise _UnavailableError(msg) from None
    if not isinstance(data, dict):
        msg = "output is not a JSON object"
        raise _UnavailableError(msg)
    return data


def _names(value: object) -> tuple[str, ...] | None:
    """*value* as a non-empty tuple of strings, else ``None``."""
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, str) for item in value)
    ):
        return tuple(str(item) for item in value)
    return None


def _gate_entries(raw: object, what: str) -> tuple[DeferredGate, ...]:
    """``[{"gate", "checks"}]`` entries; *what* names the list in a failure."""
    if not isinstance(raw, list):
        msg = f"output has no {what} list"
        raise _UnavailableError(msg)
    entries: list[DeferredGate] = []
    for item in raw:
        gate = item.get("gate") if isinstance(item, dict) else None
        checks = _names(item.get("checks")) if isinstance(item, dict) else None
        if not isinstance(gate, str) or checks is None:
            msg = f"output has a malformed {what} entry"
            raise _UnavailableError(msg)
        entries.append(DeferredGate(gate, checks))
    return tuple(entries)


def _covered(entries: tuple[DeferredGate, ...], prefix: str) -> tuple[str, ...]:
    return tuple(f"{prefix}{e.gate} via {', '.join(e.checks)}" for e in entries)


def _release_stale_hold(worktree: Path) -> None:
    """Release a stale hold before local gates can lead to an unarmed PR."""
    try:
        script = _probe_arm_script(worktree)
    except RepairStopError:
        return
    if not _supports_hold(script):
        return
    cause = _hold_release(script, worktree)
    if cause is not None:
        raise _HoldReleaseError(cause)


def consult_create(
    script: Path, worktree: Path, base: str
) -> tuple[tuple[DeferredGate, ...], list[str]]:
    """Ask ``ci-deferral`` which detected gates the base branch maps to CI.

    ``(deferred gates, friction)``. Unavailable means nothing is deferred (every
    gate runs locally) and the friction names the cause.
    """
    try:
        data = _call(script, worktree, [CI_DEFERRAL_COMMAND, "--base", base])
        deferred = _gate_entries(data.get("deferred"), "deferred")
    except _UnavailableError as exc:
        _release_stale_hold(worktree)
        return (), [_unavailable(CI_DEFERRAL_COMMAND, exc.cause)]
    return deferred, list(_covered(deferred, CREATE_DEFERRED_PREFIX))


def _parse_park(raw: object) -> ReusePark | None:
    """``ci-reuse-plan``'s ``park`` value: ``None`` or a well-formed park."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        msg = "output has a malformed park"
        raise _UnavailableError(msg)
    flavor = raw.get("flavor")
    checks = _names(raw.get("checks"))
    texts = [raw.get(k) for k in ("gate", "failed_check", "check_state", "sha")]
    url = raw.get("failed_url")
    if (
        flavor not in _PARK_FLAVORS
        or checks is None
        or not all(isinstance(text, str) for text in texts)
        or not (url is None or isinstance(url, str))
    ):
        msg = "output has a malformed park"
        raise _UnavailableError(msg)
    gate, failed_check, check_state, sha = (str(text) for text in texts)
    return ReusePark(
        "red" if flavor == "red" else "pending",
        gate,
        checks,
        failed_check,
        check_state,
        url,
        sha,
    )


def _reuse_plan(script: Path, worktree: Path, ctx: CiContext) -> ReusePlan:
    """The decoded ``ci-reuse-plan``; :class:`_UnavailableError` when unusable."""
    if ctx.pr_number is None:
        msg = "no PR number"
        raise _UnavailableError(msg)
    data = _call(
        script,
        worktree,
        [CI_REUSE_PLAN_COMMAND, "--pr", str(ctx.pr_number), "--base", ctx.base],
    )
    if "park" not in data:
        msg = "output has no park key"
        raise _UnavailableError(msg)
    skipped = _gate_entries(data.get("skip"), "skip")
    park = _parse_park(data["park"])
    if park is not None:
        return ReusePlan(park=park)
    return ReusePlan(
        skip_names=frozenset(entry.gate for entry in skipped),
        friction=_covered(skipped, REUSE_COVERED_PREFIX),
    )


def consult_reuse(script: Path, worktree: Path, ctx: CiContext) -> ReusePlan:
    """Ask ``ci-reuse-plan`` to skip, park or run each detected gate.

    Unavailable (including no PR number) is an empty plan: every gate runs
    locally, with the cause in the plan's friction. A park never runs a gate.
    """
    try:
        return _reuse_plan(script, worktree, ctx)
    except _UnavailableError as exc:
        return ReusePlan(friction=(_unavailable(CI_REUSE_PLAN_COMMAND, exc.cause),))


# -- the park ------------------------------------------------------------------


def ci_pr_info(ctx: CiContext) -> dict[str, object]:
    """The park's ``pr_info`` (the ``gate_timeout`` shape): never armed."""
    return {
        "number": ctx.pr_number,
        "url": ctx.pr_url,
        "auto_merge": False,
        "base": ctx.base,
    }


def _lead(number: object, url: object, head: str | None) -> str:
    return f"PR #{number} ({url}) at head {head or 'unknown'}"


def _details(
    flavor: BlockFlavor,
    lead: str,
    deferred: tuple[DeferredGate, ...],
    park: ReusePark | None,
    cause: str,
) -> str:
    if flavor == "deferred":
        gates = "; ".join(
            f"gate {d.gate} mapped to check(s) {', '.join(d.checks)} deferred to "
            "CI and not run locally"
            for d in deferred
        )
        return f"ci_evidence_deferred: {lead}: {gates}. {_NOT_ARMED}."
    if flavor == "hold_error":
        return (
            f"ci_evidence_hold_error: {lead}: {CI_HOLD_COMMAND} "
            f"{CI_HOLD_RELEASE_ACTION} failed ({cause}). {_NOT_ARMED}."
        )
    if park is None:
        msg = f"a {flavor} park needs its ReusePark"
        raise ValueError(msg)
    mapped = f"gate {park.gate} mapped to check {park.failed_check}"
    if flavor == "pending":
        return (
            f"ci_evidence_pending: {lead}: {mapped} is {park.check_state}; "
            f"{_NOT_ARMED} and {_NOT_RUN}."
        )
    return (
        f"ci_evidence_red: {lead}: {mapped} concluded {park.check_state} "
        f"({park.failed_url or 'no url'}). This is a failing CI check, not a "
        f"gate failure; {_NOT_ARMED} and {_NOT_RUN}."
    )


def awaiting_ci_evidence_block(
    flavor: BlockFlavor,
    ctx: CiContext,
    *,
    deferred: tuple[DeferredGate, ...] = (),
    park: ReusePark | None = None,
    cause: str = "",
) -> FinalizeBlock:
    """The ``awaiting_ci_evidence`` park for *flavor* (the contract's verbatim text).

    Retry-eligible except ``red`` (a failing CI check, not a gate failure; the
    flag is a hint, routing never reads it). The PR exists and is unarmed.
    """
    head = park.sha if park is not None else ctx.head
    lead = _lead(ctx.pr_number, ctx.pr_url, head)
    return FinalizeBlock(
        AWAITING_CI_EVIDENCE,
        _details(flavor, lead, deferred, park, redact_and_cap(cause)),
        retry_eligible=flavor != "red",
        stage=STAGE5_POST_CREATE,
        pr_info=ci_pr_info(ctx),
    )


# -- the hold ------------------------------------------------------------------


def _supports_hold(script: Path) -> bool:
    """Whether *script*'s text names ``ci-hold`` (an older copy has no hold)."""
    try:
        return _HOLD_MARKER in script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _hold_release(script: Path, worktree: Path) -> str | None:
    """Run ``ci-hold release`` and validate its JSON response."""
    done = _launch(
        script,
        worktree,
        [CI_HOLD_COMMAND, CI_HOLD_RELEASE_ACTION, "--repo-path", str(worktree)],
    )
    if isinstance(done, str):
        return done
    if done.returncode != 0:
        return _exit_cause(done)
    try:
        response = json.loads(done.stdout)
    except (ValueError, RecursionError):
        return "release output is not JSON"
    if (
        not isinstance(response, dict)
        or response.get("hold") != "released"
        or response.get("was") not in _HOLD_STATES
    ):
        return "release output does not confirm hold: released"
    return None


def release_ci_hold(
    worktree: Path, pr_info: dict[str, object], *, head: str | None = None
) -> FinalizeBlock | None:
    """Release a CI-evidence hold before a reuse pass arms, or the ``hold_error`` block.

    Resolved through the arm seam's own probe (``_probe_arm_script``). A script
    without ``ci-hold`` cannot hold anything: skipped. A missing script, a
    non-zero exit, a timeout or a launch failure blocks (never a silent skip,
    never a fall-through to the arm).
    """
    try:
        script = _probe_arm_script(worktree)
    except RepairStopError as exc:
        cause: str | None = exc.detail
    else:
        if not _supports_hold(script):
            return None
        cause = _hold_release(script, worktree)
    if cause is None:
        return None
    lead = _lead(pr_info.get("number"), pr_info.get("url"), head)
    return FinalizeBlock(
        AWAITING_CI_EVIDENCE,
        _details("hold_error", lead, (), None, redact_and_cap(cause)),
        retry_eligible=True,
        stage=STAGE5_POST_CREATE,
        pr_info={**pr_info, "auto_merge": False},
    )
