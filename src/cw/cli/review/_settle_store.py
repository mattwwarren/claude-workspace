"""Tracker-aware recording of ``cw review settle`` onto the dev-queue row (#2701).

``settle`` records its ledger on the ticket's cw dev-queue row, the ledger
every review pass reads first on every tracker. This module owns what happens
when that row cannot be recorded, which depends on the client's tracker (R1):

- **GitHub-tracked** (``tracking.primary.system`` is exactly
  ``github-issues``): degrade to mirror-only. The marker still prints, with a
  loud stderr WARNING, because the GitHub review pass reads it back off the
  thread and folds it into the row.
- **Anything else** (linear, jira, an unset or unreadable project-config, a
  client missing from ``clients.yaml``): fail with a non-zero exit and no
  marker. Nothing reads a marker there, so printing one would be the inert
  settle this ticket removes.

The polarity is the strict one the codex review pass's comment fetch
(``_context.core._fetch_ticket_comments``) uses: it reads a marker back only
on exact equality. It is NOT the fail-open
``lifecycle._tracker_allows_github_fetch``, whose unknown-means-GitHub answer
would degrade exactly where no marker is ever read.

Kept out of :mod:`cw.cli.review.commands`, which already carries all of
``settle``'s payload, provenance and audit logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.config import get_client
from cw.dev_queue.review_records import (
    TERMINAL_ROW_STATUSES,
    find_ticket_row,
    record_finding_dispositions,
)
from cw.exceptions import CwError
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker

if TYPE_CHECKING:
    from cw.review_finding_dispositions import FindingDisposition

_DEGRADED_WARNING = (
    "WARNING: this settle was NOT recorded on the cw dev-queue row for ticket "
    "'{ticket}' (client '{client}'): {reason}. The marker on stdout is the only "
    "record of it, so post it as its own comment, unedited: the GitHub codex "
    "lane reads it back and folds it into the row on the next review pass, and "
    "`cw review dispositions` will not list it until then."
)
_NO_ROW_REFUSAL = (
    "Cannot record this settle: {reason} Tracker for client '{client}' is "
    "{tracker_label}, not github-issues, so a printed marker is never read "
    "back and the settle would be inert. Nothing was recorded: no audit event, "
    "no marker, no --out file. Enqueue the ticket and re-run the same payload."
)
_WRITE_FAILED = (
    "The settle audit events were recorded, but the dev-queue row write "
    "failed: {reason}. Tracker for client '{client}' is {tracker_label}, not "
    "github-issues, so a printed marker would never be read back and the "
    "settle would be inert. No marker and no --out file were written. Re-run "
    "the same payload once the cause is fixed."
)
_TERMINAL_ROW_NOTE = (
    "Note: ticket '{ticket}' only has a terminal row ({status}). A settle "
    "recorded on it is lost if the ticket is re-enqueued (`cw dev-queue add` "
    "creates a new row and nothing is carried forward): re-run this settle on "
    "the new row."
)
_NOT_APPLIED_NOTE = (
    "Note: {n} settled finding(s) were not applied to the row because it "
    "already holds a newer record for the same key: {keys}. Check "
    "`cw review dispositions {ticket} -c {client}`."
)
#: ``tracker_label`` when the project-config names no tracker system.
_UNSET_TRACKER_LABEL = "unset (no tracking.primary.system in its project-config)"
#: ``tracker_label`` when the client itself cannot be resolved.
_UNRESOLVABLE_TRACKER_LABEL = "unresolvable (the client is not configured)"


class _ClientTracker(NamedTuple):
    """Whether a client is GitHub-tracked, and how a message names its tracker."""

    github: bool
    label: str


class SettleRowOutcome(NamedTuple):
    """How the row write went, for the caller to report on stderr."""

    #: Why the row was NOT recorded on a GitHub-tracked client, or ``None``
    #: when it was. A non-GitHub failure raises instead of returning one.
    degraded_reason: str | None
    #: Notes about a successful write (terminal row, not-applied keys).
    notes: tuple[str, ...] = ()


def _client_tracker(client: str) -> _ClientTracker:
    """Resolve *client*'s tracker with the strict GitHub polarity (R1).

    The same root ``approve.py`` and ``requeue.py`` resolve it from (the
    client's ``workspace_path``). An unconfigured client is not GitHub: fail
    closed.
    """
    try:
        workspace = get_client(client).workspace_path
    except CwError:
        return _ClientTracker(github=False, label=_UNRESOLVABLE_TRACKER_LABEL)
    system = resolve_tracker(workspace)
    if system is None:
        return _ClientTracker(github=False, label=_UNSET_TRACKER_LABEL)
    return _ClientTracker(github=system == TRACKER_GITHUB_ISSUES, label=repr(system))


def _reason(exc: BaseException) -> str:
    """*exc* as a clause: its message without a trailing period, never blank."""
    return str(exc).rstrip(".") or exc.__class__.__name__


def preflight_settle_row(ticket: str, client: str) -> str | None:
    """Check *ticket*'s row exists before any event is recorded (lock-free).

    Returns ``None`` when the row exists, or the reason it does not when the
    client is GitHub-tracked (the settle then degrades to mirror-only).

    Raises:
        CwError: on any other tracker, so nothing is recorded: no event, no
            marker, no ``--out`` file.
    """
    try:
        find_ticket_row(ticket, client)
    except (CwError, OSError) as exc:
        tracker = _client_tracker(client)
        if tracker.github:
            return _reason(exc)
        msg = _NO_ROW_REFUSAL.format(
            reason=f"{_reason(exc)}.", client=client, tracker_label=tracker.label
        )
        raise CwError(msg) from exc
    return None


def record_settle_on_row(
    ledger: dict[str, FindingDisposition], ticket: str, client: str
) -> SettleRowOutcome:
    """Merge *ledger* onto *ticket*'s row; degrade or fail by tracker (R1).

    Bounded lock: ``settle`` is an operator entry point, so a wedged holder
    fails it fast instead of hanging it. A ``LockTimeoutError`` is a
    ``CwError``, so it lands with a vanished row and an unwritable store.

    Raises:
        CwError: when the write fails and the client is not GitHub-tracked.
            The audit events already exist at that point; the message says so.
    """
    try:
        recorded = record_finding_dispositions(ticket, client, ledger, bounded=True)
    except (CwError, OSError) as exc:
        tracker = _client_tracker(client)
        if tracker.github:
            return SettleRowOutcome(degraded_reason=_reason(exc))
        msg = _WRITE_FAILED.format(
            reason=_reason(exc), client=client, tracker_label=tracker.label
        )
        raise CwError(msg) from exc
    notes: list[str] = []
    if recorded.row_status in TERMINAL_ROW_STATUSES:
        notes.append(
            _TERMINAL_ROW_NOTE.format(ticket=ticket, status=recorded.row_status.value)
        )
    if recorded.not_applied:
        notes.append(
            _NOT_APPLIED_NOTE.format(
                n=len(recorded.not_applied),
                keys=", ".join(recorded.not_applied),
                ticket=ticket,
                client=client,
            )
        )
    return SettleRowOutcome(degraded_reason=None, notes=tuple(notes))


def settle_stderr_lines(
    outcome: SettleRowOutcome, ticket: str, client: str
) -> list[str]:
    """The stderr lines for *outcome*: the degrade WARNING first, then notes."""
    lines: list[str] = []
    if outcome.degraded_reason is not None:
        lines.append(
            _DEGRADED_WARNING.format(
                ticket=ticket, client=client, reason=outcome.degraded_reason
            )
        )
    lines.extend(outcome.notes)
    return lines
