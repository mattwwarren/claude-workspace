"""``cw dev-queue add --base``: origin check and optional epic size ceiling (#3783).

RFC 0020 B1. A row enqueued with ``--base <epic>`` merges into that epic branch
rather than the client default. Before the row is stamped, three refusals run,
in this order, all outside every lock (the caller invokes them before
``add_ticket`` takes the file lock):

1. the base must not equal the client default (a null-base row already merges
   there, so the flag would be redundant);
2. the base must exist on origin, checked with one bounded ``ls-remote``;
3. when the client sets ``epic.max_diff_lines``, the base's changed-line count
   against the default must not exceed that ceiling.

The dispatch tick never reads ``epic.max_diff_lines`` (D-5); this module is its
only reader.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from cw.exceptions import CwError, GitTimeoutError
from cw.worktree import (
    FetchOutcome,
    FetchResult,
    _first_line,
    _git_dir,
    _parse_numstat_totals,
    _run_git,
)
from cw.worktree._freshness import fetch_base_branch, fetch_default_branch

if TYPE_CHECKING:
    from cw.models import ClientConfig

#: Bound on the one ``ls-remote`` origin check. An operator command, so a hung
#: remote fails closed in seconds rather than blocking the enqueue.
ENQUEUE_NET_TIMEOUT_S: Final = 30.0

#: ``git ls-remote --exit-code`` exits 2 when no ref matches the pattern.
_LS_REMOTE_NO_MATCH: Final = 2


class EpicBaseRefusedError(CwError):
    """``--base`` refused: redundant, absent on origin, unverifiable or over size.

    Defined beside its raisers (the ``BaseBranchFrozenError`` precedent) so
    ``cw.exceptions`` does not grow. ``handle_errors`` turns it into exit 1.
    """

    __slots__ = ()


def refuse_redundant_base(client: ClientConfig, base: str) -> None:
    """Refuse a ``--base`` equal to the client's default branch."""
    # stays on client.default_branch: a base equal to the client default is
    # redundant; a null-base row already merges there.
    default = client.default_branch
    if base == default:
        msg = (
            f"Refusing --base '{base}': it is the default branch of client"
            f" '{client.name}'; a row without --base already merges there."
            " Drop --base."
        )
        raise EpicBaseRefusedError(msg)


def verify_base_on_origin(client: ClientConfig, base: str) -> None:
    """Refuse unless ``refs/heads/<base>`` exists on origin, failing closed.

    One bounded, read-only ``ls-remote``. The exact ``refs/heads/<base>`` compare
    matters: ``ls-remote`` matches a pattern by its tail, so asking for ``epic/x``
    would otherwise also match ``refs/heads/x/epic/x``.
    """
    try:
        result = _run_git(
            "ls-remote",
            "--exit-code",
            "--heads",
            "origin",
            base,
            cwd=_git_dir(client),
            check=False,
            timeout=ENQUEUE_NET_TIMEOUT_S,
        )
    except GitTimeoutError:
        reason = f"git ls-remote timed out after {ENQUEUE_NET_TIMEOUT_S:g}s"
        raise _could_not_verify(base, reason) from None
    except OSError as exc:
        raise _could_not_verify(base, str(exc)) from exc
    if result.returncode == _LS_REMOTE_NO_MATCH:
        raise _absent(client, base)
    if result.returncode != 0:
        stderr_line = _first_line(result.stderr or "") or "no output"
        reason = f"git ls-remote exited {result.returncode}: {stderr_line}"
        raise _could_not_verify(base, reason)
    wanted = f"refs/heads/{base}"
    heads = [
        line.split("\t", 1)[1] for line in result.stdout.splitlines() if "\t" in line
    ]
    if wanted not in heads:
        raise _absent(client, base)


def _absent(client: ClientConfig, base: str) -> EpicBaseRefusedError:
    msg = (
        f"Refusing --base '{base}': no such branch on origin for client"
        f" '{client.name}'; push `{base}` to origin first; the orchestration"
        " skill creates it, RFC 0020 D-6."
    )
    return EpicBaseRefusedError(msg)


def _could_not_verify(base: str, reason: str) -> EpicBaseRefusedError:
    msg = (
        f"Refusing --base '{base}': could not verify `{base}` on origin;"
        f" retry. ({reason})"
    )
    return EpicBaseRefusedError(msg)


def _cannot_measure(base: str, default: str, detail: str) -> EpicBaseRefusedError:
    msg = (
        f"Refusing to enqueue on base '{base}': cannot measure its size against"
        f" '{default}' ({detail}); this client sets epic.max_diff_lines, so an"
        " unmeasured base is refused. Retry once origin is reachable."
    )
    return EpicBaseRefusedError(msg)


def epic_ceiling(client: ClientConfig) -> int | None:
    """``epic.max_diff_lines``, or None when the client sets no ceiling."""
    return client.epic.max_diff_lines


def epic_numstat_args(client: ClientConfig, base: str) -> list[str]:
    """The argv of the three-dot numstat measure of *base* against the default.

    Changes on *base* since its merge-base with the default (a commit that
    landed only on the default is not counted), with ``merge_gate_ignore_paths``
    excluded. Shared by enqueue (this module) and the doctor's advisory, so the
    two report the same number.
    """
    # stays on client.default_branch: the epic is measured against the client's
    # landing branch, the default.
    default = client.default_branch
    excludes = [f":(top,literal,exclude){p}" for p in client.merge_gate_ignore_paths]
    return [
        "diff",
        "--numstat",
        "--no-renames",
        f"origin/{default}...origin/{base}",
        "--",
        ".",
        *excludes,
    ]


def check_epic_size_ceiling(client: ClientConfig, base: str) -> None:
    """Refuse when ``base`` is over ``epic.max_diff_lines`` against the default.

    Unset ceiling means no git call at all. Otherwise both refs are fetched and
    the three-dot numstat is taken (changes on ``base`` since its merge-base with
    the default, so a commit that landed only on the default is not counted),
    with ``merge_gate_ignore_paths`` excluded. Any measurement failure refuses:
    the operator opted into a ceiling, so an unmeasured base is not enqueued.
    """
    ceiling = epic_ceiling(client)
    if ceiling is None:
        return
    # stays on client.default_branch: the epic is measured against the client's
    # landing branch, the default.
    default = client.default_branch
    default_fetch = fetch_default_branch(client)
    if default_fetch.outcome is not FetchOutcome.FETCHED:
        detail = f"fetch of origin/{default} failed: {_fetch_reason(default_fetch)}"
        raise _cannot_measure(base, default, detail)
    base_fetch = fetch_base_branch(client, base)
    if base_fetch.outcome is FetchOutcome.BRANCH_ABSENT:
        raise _cannot_measure(base, default, f"origin/{base} is absent after fetch")
    if base_fetch.outcome is not FetchOutcome.FETCHED:
        detail = f"fetch of origin/{base} failed: {_fetch_reason(base_fetch)}"
        raise _cannot_measure(base, default, detail)
    result = _run_git(
        *epic_numstat_args(client, base),
        cwd=_git_dir(client),
        check=False,
    )
    if result.returncode != 0:
        stderr_line = _first_line(result.stderr or "") or "no output"
        detail = f"git diff exited {result.returncode}: {stderr_line}"
        raise _cannot_measure(base, default, detail)
    _files, lines = _parse_numstat_totals(result.stdout)
    if lines > ceiling:
        msg = (
            f"Refusing to enqueue on base '{base}': {lines} changed lines"
            f" (merge-base diff against '{default}', additions plus deletions,"
            " merge_gate_ignore_paths excluded) exceed this client's"
            f" epic.max_diff_lines of {ceiling}. Remedy: land the epic now (open"
            f" its PR to '{default}' and adopt it: cw dev-queue add <parent>"
            " --adopt-pr <N>), or open a second epic branch from"
            f" '{default}' for this ticket."
        )
        raise EpicBaseRefusedError(msg)


def _fetch_reason(result: FetchResult) -> str:
    """The fetch's stated reason, or its lower-cased outcome name when none."""
    return result.reason or result.outcome.name.lower()
