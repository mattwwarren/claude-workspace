"""``cw doctor`` ``epic-base/<client>/<branch>``: advisory per live epic base (#3786).

RFC 0020 B4. For each epic base a non-terminal row merges into, one
``CheckResult`` reports the three-dot diff size (against the client's
``epic.max_diff_lines`` ceiling when one is set), the age of the oldest
unlanded commit, and the default-branch merge probe into the epic. It also
flags a likely branch-protection misconfiguration: a child PR awaiting a
required review on that base.

Read-only and fail-open: it reads only the local ``origin/*`` refs (no fetch,
no ``gh`` call), goes through the bounded git helpers, and never raises. It
returns ``CheckResult`` values only and writes nothing. ``ok`` is always True;
``warn`` is set for an over-ceiling base, a probe conflict, or a suspected
misconfiguration. Age never warns (ADR-0014). Bases past the per-run cap or
the wall-clock budget get an advisory saying so. Leaf module: it imports only
``cw.doctor._shared`` from the doctor package.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cw.dev_queue.epic_base import epic_ceiling, epic_numstat_args
from cw.doctor._shared import CheckResult
from cw.merge_probe import (
    PROBE_TIMEOUT_SECONDS,
    GitFailedError,
    MergeProbe,
    failure_detail,
    git_bounded,
    probe_default_merge,
    resolve_base,
)
from cw.models import TERMINAL_QUEUE_STATUSES
from cw.worktree import _git_dir, _parse_numstat_totals

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from cw.models import ClientConfig, PrState, TicketTask

_CHECK_NAME: Final = "epic-base"
#: Bases measured per ``cw doctor`` run; later bases get an advisory only.
MAX_BASES_PER_RUN: Final = 8
#: Wall-clock budget for all git calls in one run.
BUDGET_SECONDS: Final = 15.0

_NO_FETCH: Final = "local origin/* refs, no fetch: as of the last fetch"
_EXHAUSTED: Final = "time budget exhausted"
_DAY_S: Final = 86_400
_HOUR_S: Final = 3_600
_OVER_REMEDY: Final = (
    "Remedy: land the epic now (open its PR to the default branch and adopt it),"
    " or open a second epic branch from the default branch for new work."
)
_MISCONFIGURED: Final = (
    "{count} child PR(s) ({ids}) await a required review: branch protection on"
    " {base} likely requires a human review. Remove that requirement (operator"
    " action, RFC 0020 D-12); cw never changes repository settings and never"
    " approves (ADR-0012)."
)


@dataclass(frozen=True)
class _Budget:
    """Wall-clock deadline over an injected monotonic clock."""

    clock: Callable[[], float]
    deadline: float

    def timeout(self) -> float | None:
        """``min(PROBE_TIMEOUT_SECONDS, remaining)``; None once exhausted."""
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            return None
        return min(PROBE_TIMEOUT_SECONDS, remaining)


@dataclass(frozen=True)
class _Measure:
    """One base's measured fields; ``None`` plus a reason where unknown."""

    lines: int | None
    lines_why: str
    oldest: int | None
    oldest_why: str
    probe: MergeProbe


def _unknown_measure(why: str) -> _Measure:
    return _Measure(None, why, None, why, MergeProbe("unavailable", None, (), why))


def _live_bases(
    clients: dict[str, ClientConfig], tasks: Sequence[TicketTask]
) -> dict[tuple[str, str], list[TicketTask]]:
    """Non-terminal rows grouped by (client, base), known clients only, sorted."""
    groups: dict[tuple[str, str], list[TicketTask]] = {}
    for row in tasks:
        if (
            row.client not in clients
            or row.base_branch is None
            or row.status in TERMINAL_QUEUE_STATUSES
        ):
            continue
        groups.setdefault((row.client, row.base_branch), []).append(row)
    return dict(sorted(groups.items()))


def _awaits_required_review(pr: PrState | None) -> bool:
    return (
        pr is not None
        and pr.state == "OPEN"
        and not pr.is_draft
        and pr.review_decision == "REVIEW_REQUIRED"
    )


def _misconfigured_ids(
    tasks: Sequence[TicketTask], client_name: str, base: str
) -> list[str]:
    """Ticket ids of child PRs on this base awaiting a required review.

    Scans every row of the base, terminal ones included: a COMPLETED child whose
    PR is still open on the epic is the typical case.
    """
    return sorted(
        row.ticket_id
        for row in tasks
        if row.client == client_name
        and row.base_branch == base
        and _awaits_required_review(row.pr_state)
    )


def _lines(
    cwd: Path, client: ClientConfig, base: str, budget: _Budget
) -> tuple[int | None, str]:
    """Three-dot numstat of *base* against the default, ignore paths excluded."""
    timeout = budget.timeout()
    if timeout is None:
        return None, _EXHAUSTED
    try:
        result = git_bounded(cwd, epic_numstat_args(client, base), timeout)
    except GitFailedError as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, failure_detail("diff", result)
    _files, lines = _parse_numstat_totals(result.stdout)
    return lines, ""


def _oldest(
    cwd: Path, default: str, base: str, budget: _Budget
) -> tuple[int | None, str]:
    """Oldest committer time (``%ct``) of the unlanded commits; None if none."""
    timeout = budget.timeout()
    if timeout is None:
        return None, _EXHAUSTED
    argv = ["log", "--format=%ct", f"origin/{default}..origin/{base}"]
    try:
        result = git_bounded(cwd, argv, timeout)
    except GitFailedError as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, failure_detail("log", result)
    stamps = [int(stamp) for stamp in result.stdout.split()]
    return (min(stamps), "") if stamps else (None, "")


def _probe(cwd: Path, default: str, epic_sha: str, budget: _Budget) -> MergeProbe:
    """The default merged into the epic head, its three calls sharing the budget."""
    timeout = budget.timeout()
    if timeout is None:
        return MergeProbe("unavailable", None, (), _EXHAUSTED)
    return probe_default_merge(cwd, default, timeout=timeout / 3, head=epic_sha)


def _measure(client: ClientConfig, base: str, budget: _Budget) -> _Measure:
    """Measure one base. Every field fails open to ``unknown`` with its reason."""
    # stays on client.default_branch: the epic is measured against the client's
    # landing branch, the default.
    default = client.default_branch
    cwd = _git_dir(client)
    timeout = budget.timeout()
    if timeout is None:
        return _unknown_measure(_EXHAUSTED)
    try:
        epic_sha = resolve_base(cwd, base, timeout)
    except GitFailedError as exc:
        return _unknown_measure(str(exc))
    lines, lines_why = _lines(cwd, client, base, budget)
    oldest, oldest_why = _oldest(cwd, default, base, budget)
    probe = _probe(cwd, default, epic_sha, budget)
    return _Measure(lines, lines_why, oldest, oldest_why, probe)


def _age_words(seconds: float) -> str:
    days, rest = divmod(int(seconds), _DAY_S)
    hours = rest // _HOUR_S
    parts: list[str] = []
    if days:
        parts.append(f"{days} day{'' if days == 1 else 's'}")
    if hours or not days:
        parts.append(f"{hours} hour{'' if hours == 1 else 's'}")
    return " ".join(parts)


def _unknown(why: str) -> str:
    return f"unknown ({why})"


def _diff_text(measure: _Measure, ceiling: int | None) -> tuple[str, bool]:
    """The diff clause and whether the base is over its ceiling."""
    if measure.lines is None:
        return f"diff {_unknown(measure.lines_why)}", False
    if ceiling is None:
        return f"diff {measure.lines} lines; no epic ceiling configured", False
    if measure.lines > ceiling:
        return (
            f"diff {measure.lines} lines; ceiling {ceiling}: OVER. {_OVER_REMEDY}",
            True,
        )
    return f"diff {measure.lines} lines; ceiling {ceiling}: within", False


def _age_text(measure: _Measure, now: float) -> str:
    if measure.oldest is not None:
        return _age_words(max(0.0, now - measure.oldest))
    if measure.oldest_why:
        return _unknown(measure.oldest_why)
    return "none unlanded"


def _probe_text(measure: _Measure, default: str, base: str) -> str:
    head = f"merge probe ({default} into {base}): "
    probe = measure.probe
    if probe.state == "unavailable":
        return head + _unknown(probe.detail)
    if probe.state == "conflict":
        return head + f"conflict: {', '.join(probe.conflicted_files)}"
    if probe.state == "up_to_date":
        return head + "up to date"
    return head + "clean"


def _result(
    client_name: str,
    base: str,
    client: ClientConfig,
    rows: Sequence[TicketTask],
    measure: _Measure | None,
    skipped: str,
    misconfigured: Sequence[str],
    now: float,
) -> CheckResult:
    # stays on client.default_branch: the advisory names the landing branch.
    default = client.default_branch
    live = ", ".join(sorted(row.ticket_id for row in rows))
    over = False
    if measure is None:
        parts = [f"{base} vs {default} ({_NO_FETCH}): {skipped}"]
    else:
        diff, over = _diff_text(measure, epic_ceiling(client))
        parts = [
            f"{base} vs {default} ({_NO_FETCH})",
            diff,
            f"oldest unlanded commit {_age_text(measure, now)}",
            _probe_text(measure, default, base),
        ]
    parts.append(f"live rows: {live}")
    if misconfigured:
        parts.append(
            _MISCONFIGURED.format(
                count=len(misconfigured), ids=", ".join(misconfigured), base=base
            )
        )
    conflict = measure is not None and measure.probe.state == "conflict"
    return CheckResult(
        f"{_CHECK_NAME}/{client_name}/{base}",
        ok=True,
        warn=over or conflict or bool(misconfigured),
        detail="; ".join(parts),
    )


def _check_epic_base(
    clients: dict[str, ClientConfig],
    tasks: Sequence[TicketTask],
    *,
    now: float | None = None,
    max_bases: int = MAX_BASES_PER_RUN,
    budget_s: float = BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[CheckResult]:
    """One advisory per live epic base; never raises, never mutates the repo."""
    current = time.time() if now is None else now
    budget = _Budget(monotonic, monotonic() + budget_s)
    results: list[CheckResult] = []
    for index, ((client_name, base), rows) in enumerate(
        _live_bases(clients, tasks).items()
    ):
        client = clients[client_name]
        misconfigured = _misconfigured_ids(tasks, client_name, base)
        measure: _Measure | None = None
        skipped = ""
        if index >= max_bases:
            skipped = f"not measured (per-run cap of {max_bases} bases)"
        elif budget.timeout() is None:
            skipped = f"not measured ({_EXHAUSTED})"
        else:
            measure = _measure(client, base, budget)
        results.append(
            _result(
                client_name,
                base,
                client,
                rows,
                measure,
                skipped,
                misconfigured,
                current,
            )
        )
    return results
