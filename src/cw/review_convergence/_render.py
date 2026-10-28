"""Renderers and summaries of the converge-to-clean ledger (#2787).

Everything an operator, the tracker, the sentinel or the prose reads off a
ledger: the history comment, the friction lines, the stop summary with its
ready-to-paste ``operator-settle`` payload, the sentinel ``review`` fragment,
the confirm-backend blocker fragment and the ``review.finding_settled`` audit
payload. All pure, with no clock and no I/O.

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cw.auto_dev_result.schema._vocab import (
    CONFIRM_BACKEND_UNAVAILABLE_BLOCKER_REASON,
    STAGE3_REVIEW,
)
from cw.gh import AGENT_COMMENT_MARKER
from cw.review_convergence._models import (
    LEDGER_STATUS_CONVERGED,
    LEDGER_STATUS_STOPPED,
    STOP_MAX_CYCLES,
    STOP_NO_PROGRESS,
    STOP_OPERATOR_ACTION,
    STOP_OSCILLATION,
)
from cw.review_convergence._policy import (
    consecutive_confirm_failures,
    release_cause,
)
from cw.review_markers import escape_angle_brackets_in_json, neutralise_marker_syntax

if TYPE_CHECKING:
    from cw.review_convergence._models import ConvergenceLedger, SettledEntry

#: Longest text :func:`one_line` returns before the ellipsis.
ONE_LINE_LIMIT = 300


def one_line(text: str, limit: int = ONE_LINE_LIMIT) -> str:
    """*text* with whitespace collapsed to single spaces, capped at *limit*."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "..."


_REQUEUE = "cw dev-queue requeue <T> -c <C>"
_REGRESS_REQUEUE = f"{_REQUEUE} --stage impl --regress"
_TERMINAL_HINT = (
    f"terminal until a commit lands: land real commits, then {_REGRESS_REQUEUE}"
)

#: ``stop.release_hint``: a fixed string per stop reason (R11 to R13). Any
#: reason not listed is terminal, like ``no_progress``.
_RELEASE_HINTS: dict[str, str] = {
    STOP_MAX_CYCLES: (
        f"an operator raises review.max_cycles for the client, then {_REQUEUE}"
    ),
    STOP_OPERATOR_ACTION: (
        "execute the operator checklist; first run 'cw review converge "
        "operator-settle' for any finding the reviewer would re-raise (payload "
        f"in settle_payload), then {_REQUEUE}"
    ),
    STOP_OSCILLATION: (
        "run 'cw review converge operator-settle' for every recurring finding "
        f"that is a false positive (payload in settle_payload), then {_REQUEUE}; "
        f"or land a commit that fixes the code: {_REGRESS_REQUEUE}"
    ),
    STOP_NO_PROGRESS: _TERMINAL_HINT,
}

#: Abbreviated sha length in rendered text.
_SHORT_SHA = 7


def _short(sha: str | None) -> str:
    return (sha or "")[:_SHORT_SHA]


def _text(value: str) -> str:
    """Single-line *value* with comment-marker syntax neutralised."""
    return neutralise_marker_syntax(" ".join(value.split()))


def settle_payload(ledger: ConvergenceLedger) -> dict[str, object]:
    """A ready-to-paste ``operator-settle`` payload over ``stop_identities``.

    Uses each identity's normalized summary, which matches because identity
    normalizes. Empty (``{}``) when the stop recorded no identities.
    """
    if not ledger.stop_identities:
        return {}
    return {
        "entries": [
            {
                "severity": identity.severity,
                "file": identity.file,
                "summary": identity.summary_normalized,
            }
            for identity in ledger.stop_identities
        ]
    }


def stop_summary(ledger: ConvergenceLedger) -> dict[str, object] | None:
    """The ``stop`` object of ``converge status``, or ``None`` unless stopped.

    ``{reason, identities, settle_payload, releasable, release_hint}``:
    ``releasable`` is whether a same-tip requeue would release the stop right
    now, and ``release_hint`` is the fixed per-reason instruction for what
    releases it.
    """
    if ledger.status != LEDGER_STATUS_STOPPED:
        return None
    reason = ledger.stop_reason or ""
    return {
        "reason": reason,
        "identities": [i.model_dump(mode="json") for i in ledger.stop_identities],
        "settle_payload": settle_payload(ledger),
        "releasable": release_cause(ledger) is not None,
        "release_hint": _RELEASE_HINTS.get(reason, _TERMINAL_HINT),
    }


def operator_settle_event_payload(entry: SettledEntry) -> dict[str, object]:
    """The ``review.finding_settled`` payload of one operator-settled identity.

    The eight keys ``disposition_event_payload`` emits, except that ``key`` is
    the identity's ``audit_key()`` (severity-aware, and defined for a
    ``file="N/A"`` finding), ``file`` is the finding's file verbatim, and
    ``outcome`` is ``"REJECTED"``. ``summary`` is the identity's
    whitespace-normalized summary (the ledger keeps no other copy).
    """
    return {
        "key": entry.identity.audit_key(),
        "file": entry.identity.file,
        "summary": entry.identity.summary_normalized,
        "outcome": "REJECTED",
        "reason": entry.rationale,
        "actor": entry.actor,
        "recorded_at": entry.settled_at,
        "reviewed_sha": entry.reviewed_sha,
    }


def friction_lines(ledger: ConvergenceLedger) -> list[str]:
    """The ``friction_highlights`` strings the ledger alone can produce.

    One ``review_round_<n>`` line per recorded round (``settled`` counts the
    round's newly settled plus resurfaced settled findings), then
    ``deferred_audited`` (every settled identity), ``operator_settled`` (the
    operator-origin ones, with distinct actors), ``confirm_not_independent`` and
    ``confirm_round_opt_out``. Strings that need session context (a park, an
    exit code) are appended by the prose at the step that knows them.
    """
    lines = [
        (
            f"review_round_{r.round}: kind={r.kind} backend={r.backend} "
            f"sha={_short(r.reviewed_sha)} new_accepted={r.new_accepted} "
            f"settled={r.new_settled + r.resurfaced_settled}"
        )
        for r in ledger.rounds
    ]
    if ledger.settled:
        lines.append(
            f"deferred_audited: {len(ledger.settled)} deferred/rejected "
            "finding(s) settled without re-review; see convergence history comment"
        )
    operator = [e for e in ledger.settled if e.origin == "operator"]
    if operator:
        actors = sorted({e.actor or "unknown" for e in operator})
        lines.append(
            f"operator_settled: {len(operator)} finding(s) settled by operator "
            f"({', '.join(actors)}); see convergence history comment"
        )
    if ledger.confirm_not_independent:
        lines.append(
            f"confirm_not_independent: confirm_backend={ledger.confirm_backend} "
            f"equals loop backend {ledger.loop_backend}; no independent round ran"
        )
    if ledger.confirm_backend == "none":
        lines.append("confirm_round_opt_out: review.confirm_backend=none")
    return lines


def sentinel_review_fragment(ledger: ConvergenceLedger) -> dict[str, object]:
    """The clean-mode fields of the sentinel's ``review`` object.

    The five new fields (``convergence_mode``, ``rounds_run``, ``converged``,
    ``final_round_clean_sha``, ``confirm_backend``) and ``fix_cycles_used``,
    plus the frozen cycle-0 ``must_fix_initial``, ``should_fix``,
    ``agents_run`` and ``rejected_*`` counts when the ledger has them.
    ``final_round_clean_sha`` is set only for a converged ledger.
    """
    converged = ledger.status == LEDGER_STATUS_CONVERGED
    fragment: dict[str, object] = {
        "convergence_mode": "clean",
        "rounds_run": len(ledger.rounds),
        "converged": converged,
        "final_round_clean_sha": ledger.last_clean_sha if converged else None,
        "confirm_backend": ledger.confirm_backend,
        "fix_cycles_used": ledger.fix_cycles_used,
    }
    if ledger.frozen is not None:
        fragment.update(ledger.frozen.model_dump(mode="json"))
    return fragment


def confirm_blocker_fragment(
    ledger: ConvergenceLedger, *, escalated: bool, client: str | None = None
) -> dict[str, object]:
    """The sentinel ``blocker`` for the latest confirm-backend failure.

    ``escalated=False`` is the uncharged ``confirm_backend_unavailable`` park
    (``retry_eligible: true``). ``escalated=True`` (R26, the fourth same-cause
    failure at one sha) is an ``agent_block`` outside the uncharged class.
    *client* fills the requeue hint's ``-c`` (a ``<C>`` placeholder when
    omitted). Needs at least one recorded confirm failure.
    """
    failure = ledger.confirm_failures[-1]
    requeue = f"cw dev-queue requeue {ledger.ticket_id} -c {client or '<C>'}"
    if not escalated:
        return {
            "stage": STAGE3_REVIEW,
            "reason": CONFIRM_BACKEND_UNAVAILABLE_BLOCKER_REASON,
            "details": f"{failure.cause}: {failure.detail}",
            "recovery_hint": f"restore the confirm backend, then {requeue}",
            "retry_eligible": True,
            "retry_delay_seconds": None,
        }
    count = consecutive_confirm_failures(ledger, cause=failure.cause)
    return {
        "stage": STAGE3_REVIEW,
        "reason": "agent_block",
        "details": (
            f"confirm backend unavailable {count} times in a row with cause "
            f"{failure.cause} at {_short(failure.candidate_sha)}: "
            f"{failure.detail}; the backend is not recovering, so verify it "
            "before any further requeue"
        ),
        "recovery_hint": (
            "triage with cw dev-queue show and verify the backend; this "
            "agent_block is outside the uncharged class (the charge follows the "
            f"claim evidence), so each further round needs an explicit {requeue}"
        ),
        "retry_eligible": False,
        "retry_delay_seconds": None,
    }


def _round_table(ledger: ConvergenceLedger) -> list[str]:
    lines = [
        "| Round | Kind | Backend | SHA | New accepted | Fix | Operator action "
        "| Settled | Result |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {r.round} | {r.kind} | {_text(r.backend)} | {_short(r.reviewed_sha)} "
        f"| {r.new_accepted} | {r.new_fix} | {r.new_operator_action} "
        f"| {r.new_settled + r.resurfaced_settled} "
        f"| {'clean' if r.clean else 'dirty'} |"
        for r in ledger.rounds
    )
    return lines


def _audited_item(entry: SettledEntry) -> str:
    identity = entry.identity
    head = (
        f"- {identity.severity} `{_text(identity.file)}`: "
        f"{_text(identity.summary_normalized)} "
    )
    if entry.origin == "operator":
        return (
            f"{head}(settled by operator {_text(entry.actor or 'unknown')} at "
            f"{_text(entry.settled_at or 'unknown time')}, round {entry.round}, "
            f"sha {_short(entry.reviewed_sha)}): {_text(entry.rationale)}"
        )
    return f"{head}({entry.outcome} in round {entry.round}): {_text(entry.rationale)}"


def _releases_section(ledger: ConvergenceLedger) -> list[str]:
    if not ledger.releases:
        return []
    return [
        "",
        "### Stop releases",
        "",
        *(
            f"- round {r.round}: released `{_text(r.reason)}` via {r.cause} "
            f"at {_short(r.sha)} ({_text(r.at)})"
            for r in ledger.releases
        ),
    ]


def _confirm_failures_section(ledger: ConvergenceLedger) -> list[str]:
    if not ledger.confirm_failures:
        return []
    return [
        "",
        "### Confirm backend failures",
        "",
        *(
            f"- {_text(f.at)}: {_text(f.cause)} at {_short(f.candidate_sha)} "
            f"(after round {f.round}): {_text(f.detail)}"
            for f in ledger.confirm_failures
        ),
    ]


def _stop_section(ledger: ConvergenceLedger, stop_reason: str) -> list[str]:
    summary = stop_summary(ledger)
    hint = summary["release_hint"] if summary else _TERMINAL_HINT
    lines = ["", f"### Stopped: {_text(stop_reason)}", "", f"Release: {hint}"]
    if ledger.stop_identities:
        lines.extend(["", "Recurring or triggering findings:", ""])
        lines.extend(
            f"- {i.severity} `{_text(i.file)}`: {_text(i.summary_normalized)}"
            for i in ledger.stop_identities
        )
    payload = settle_payload(ledger)
    if payload:
        rendered = escape_angle_brackets_in_json(json.dumps(payload, indent=2))
        lines.extend(
            [
                "",
                "To settle a finding that is a false positive, save this payload "
                "and run `cw review converge operator-settle <payload.json> "
                '--ticket-id <T> -c <C> --reason "<why>"`:',
                "",
                "```json",
                rendered,
                "```",
            ]
        )
    return lines


def render_history_comment(
    ledger: ConvergenceLedger, stop_reason: str | None = None
) -> str:
    """The ``## Review Convergence History`` tracker comment.

    A per-round table, the audited "Deferred / rejected" list (operator settles
    included, with actor, time and reason), stop releases, confirm-backend
    failures and, when *stop_reason* is given, the stop with its release hint,
    its recurring identities and a ready-to-paste ``operator-settle`` payload.
    Finding text goes through ``neutralise_marker_syntax`` so a summary cannot
    carry a parseable marker block, and the comment ends with the agent
    provenance marker.
    """
    lines = [
        "## Review Convergence History",
        "",
        f"Mode: clean | confirm backend: {ledger.confirm_backend} | rounds: "
        f"{len(ledger.rounds)} | fix cycles used: {ledger.fix_cycles_used}/"
        f"{ledger.max_cycles} | status: {ledger.status}",
        "",
        *_round_table(ledger),
    ]
    if ledger.settled:
        lines.extend(["", "### Deferred / rejected (audited)", ""])
        lines.extend(_audited_item(entry) for entry in ledger.settled)
    lines.extend(_releases_section(ledger))
    lines.extend(_confirm_failures_section(ledger))
    if stop_reason is not None:
        lines.extend(_stop_section(ledger, stop_reason))
    lines.extend(["", AGENT_COMMENT_MARKER, ""])
    return "\n".join(lines)
