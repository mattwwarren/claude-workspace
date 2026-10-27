"""``cw review converge`` -- the clean-mode review convergence CLI (#2787).

The prose command files cannot own round state (the fix loop hands off across
sessions), so the converge-to-clean state machine is deterministic Python in
:mod:`cw.review_convergence`, persisted as a ledger at
``<worktree>/.cw/review-convergence.json``. This module defines the
``converge`` group and the seven subcommands that touch only the ledger and
its payloads: ``config``, ``init``, ``suppress``, ``record-round``,
``confirm-failed``, ``status`` and ``history-comment``. The tip commands
(``verify-tip``, ``finalize``) live in :mod:`cw.cli.review.converge_tip` and the
operator ingress (``operator-settle``) in :mod:`cw.cli.review.operator_settle`;
both import the group and the shared helpers from here, so this module imports
neither.

None of these commands emits an event. A failure the operator can act on is a
:class:`~cw.exceptions.CwError` (one-line message, exit 1, nothing written); a
payload that does not validate prints ``field.path: message`` lines and exits 1,
the shape ``cw review check-voided`` uses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import click
from pydantic import BaseModel, ConfigDict, model_validator

from cw._git import run_git
from cw.atomic import atomic_write_text
from cw.cli._base import handle_errors
from cw.cli.review._group import _parse_payload_or_exit, review
from cw.cli.review.commands import _utc_now_iso
from cw.exceptions import CwError
from cw.models import HOOK_CONTEXT_RELATIVE_PATH
from cw.models.client import DEFAULT_BLOCKING_MAX_CYCLES, DEFAULT_CLEAN_MAX_CYCLES
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    ResolvedBackend,
    RoundFinding,
    RoundInput,
    confirm_blocker_fragment,
    consecutive_confirm_failures,
    frozen_counts_from_verdict,
    load_ledger,
    new_ledger,
    reconcile_ledger_for_init,
    record_confirm_failure,
    record_round,
    render_history_comment,
    resolve_confirm_backend,
    round_input_findings,
    save_ledger,
    save_round_verdict,
    sentinel_review_fragment,
    status_summary,
    stop_summary,
    suppress_settled,
)
from cw.review_findings import ReviewVerdict

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cw.review_convergence import ConfirmBackend, ConvergenceLedger, Reconciled

#: ``converge config``'s ``source`` when the context carries no ``review``
#: object (missing, unreadable, malformed, or written below schema 15), so
#: nothing says the client is clean. There is no ``fallback:`` value.
SOURCE_NO_REVIEW = "no_review_in_context"
SOURCE_CONTEXT = "context"

#: The first ``.claude/cw-context.json`` schema that carries a ``review`` object.
_REVIEW_CONTEXT_SCHEMA = 15
_DEFAULT_LOOP_BACKEND = "claude-native"
_REPORT_REASON_LIMIT = 300

DISPATCH_REFUSAL = (
    "clean-mode review runs only on a ticket dispatched through cw dev-queue "
    "(it needs a ticket id and a dev-queue row): enqueue it with "
    "`cw dev-queue add <ticket> -c <client> --lane <lane>` and let "
    "`cw dev-queue run` dispatch it"
)

_LEDGER_HELP = (
    "The convergence ledger. Default: .cw/review-convergence.json. Commands "
    "that take --worktree read a relative path from there."
)
_WORKTREE_HELP = "The worktree the ledger and the git state belong to."
_CONTEXT_HELP = "The cw context file the review settings are read from."
_LOOP_BACKEND_HELP = (
    "The review loop's own backend; picks the independent confirm backend."
)


def ledger_option[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """The shared ``--ledger`` option (default ``.cw/review-convergence.json``)."""
    return click.option(
        "--ledger",
        "ledger_rel",
        default=LEDGER_RELATIVE_PATH,
        show_default=True,
        type=click.Path(path_type=Path, dir_okay=False),
        help=_LEDGER_HELP,
    )(fn)


def worktree_option[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """The shared ``--worktree`` option (default ``.``)."""
    return click.option(
        "--worktree",
        default=Path(),
        show_default=True,
        type=click.Path(path_type=Path, file_okay=False),
        help=_WORKTREE_HELP,
    )(fn)


@review.group(name="converge")
def converge() -> None:
    """Clean-mode review convergence: ledger, rounds, stops (#2787)."""


def emit_json(body: object) -> None:
    """Print *body* as one indented JSON document."""
    click.echo(json.dumps(body, indent=2))


def _one_line(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _REPORT_REASON_LIMIT:
        return collapsed
    return collapsed[:_REPORT_REASON_LIMIT] + "..."


def load_ledger_or_none(path: Path) -> ConvergenceLedger | None:
    """The ledger at *path*, ``None`` when absent, a ``CwError`` when unreadable.

    ``load_ledger`` raises ``ValueError`` subclasses (pydantic's
    ``ValidationError``, ``UnicodeDecodeError``) on a corrupt file and
    ``OSError`` on an unreadable one; the operator gets one line, never a
    traceback.
    """
    try:
        return load_ledger(path)
    except (OSError, ValueError) as exc:
        msg = (
            f"cannot read the convergence ledger at {path}: "
            f"{type(exc).__name__}: {_one_line(str(exc))}"
        )
        raise CwError(msg) from exc


def require_ledger(path: Path) -> ConvergenceLedger:
    """The ledger at *path*, or a ``CwError`` when none exists."""
    ledger = load_ledger_or_none(path)
    if ledger is None:
        msg = f"no convergence ledger at {path}; run 'cw review converge init' first"
        raise CwError(msg)
    return ledger


def git_stdout(argv: Sequence[str], worktree: Path) -> str:
    """Stripped stdout of ``git *argv`` in *worktree*, or a ``CwError``."""
    command = f"git {' '.join(argv)}"
    try:
        result = run_git(argv, cwd=worktree, capture_output=True)
    except OSError as exc:
        msg = f"{command} could not run in {worktree}: {exc}"
        raise CwError(msg) from exc
    if result.returncode != 0:
        msg = f"{command} failed in {worktree}: {_one_line(result.stderr)}"
        raise CwError(msg)
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConvergenceConfig:
    """The clean-mode settings a cw context carries (plus where they came from)."""

    convergence: Literal["blocking", "clean"]
    max_cycles: int
    confirm_backend_configured: ConfirmBackend | None
    source: str
    ticket_id: str | None

    @property
    def dispatch_refusal(self) -> str | None:
        """The run-start refusal for a clean client with no usable ticket id (R24)."""
        if self.convergence != "clean":
            return None
        if self.ticket_id is None or not self.ticket_id.strip():
            return DISPATCH_REFUSAL
        return None


def _read_context(path: Path) -> dict[str, object] | None:
    """The context file as a JSON object, ``None`` for any unusable shape."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _context_review(context: dict[str, object] | None) -> dict[str, object] | None:
    """The ``review`` object of a schema-15-or-later context, else ``None``."""
    if context is None:
        return None
    schema = context.get("schema_version")
    review_obj = context.get("review")
    if not (
        isinstance(schema, int)
        and not isinstance(schema, bool)
        and schema >= _REVIEW_CONTEXT_SCHEMA
        and isinstance(review_obj, dict)
    ):
        return None
    return review_obj


def _configured_backend(value: object) -> ConfirmBackend | None:
    if value == "codex":
        return "codex"
    if value == "claude":
        return "claude"
    if value == "none":
        return "none"
    return None


def _cycles(value: object, *, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return default


def load_convergence_config(context_path: Path) -> ConvergenceConfig:
    """Read the review settings from *context_path*; never raises for its shape.

    A missing, unreadable, malformed or below-schema-15 context carries no
    ``review`` object, so nothing says the client is clean and it reads as
    ``blocking`` with source ``no_review_in_context``. A context that says
    ``clean`` is believed even when its other keys are odd (an unknown
    ``confirm_backend`` reads as unset), so a drifted context cannot silently
    downgrade a clean client to blocking.
    """
    context = _read_context(context_path)
    raw_ticket = None if context is None else context.get("ticket_id")
    ticket_id = raw_ticket if isinstance(raw_ticket, str) else None
    review_obj = _context_review(context)
    if review_obj is None:
        return ConvergenceConfig(
            "blocking", DEFAULT_BLOCKING_MAX_CYCLES, None, SOURCE_NO_REVIEW, ticket_id
        )
    backend = _configured_backend(review_obj.get("confirm_backend"))
    if review_obj.get("convergence") != "clean":
        return ConvergenceConfig(
            "blocking",
            _cycles(review_obj.get("max_cycles"), default=DEFAULT_BLOCKING_MAX_CYCLES),
            backend,
            SOURCE_CONTEXT,
            ticket_id,
        )
    return ConvergenceConfig(
        "clean",
        _cycles(review_obj.get("max_cycles"), default=DEFAULT_CLEAN_MAX_CYCLES),
        backend,
        SOURCE_CONTEXT,
        ticket_id,
    )


def _config_report(config: ConvergenceConfig, loop_backend: str) -> dict[str, object]:
    resolved = resolve_confirm_backend(config.confirm_backend_configured, loop_backend)
    return {
        "convergence": config.convergence,
        "max_cycles": config.max_cycles,
        "confirm_backend": resolved.name,
        "confirm_backend_configured": config.confirm_backend_configured,
        "confirm_not_independent": resolved.not_independent,
        "loop_backend": loop_backend,
        "source": config.source,
        "ticket_id": config.ticket_id,
        "dispatch_refusal": config.dispatch_refusal,
    }


@converge.command(name="config")
@click.option(
    "--context",
    "context_path",
    default=HOOK_CONTEXT_RELATIVE_PATH,
    show_default=True,
    type=click.Path(path_type=Path),
    help=_CONTEXT_HELP,
)
@click.option(
    "--loop-backend",
    default=_DEFAULT_LOOP_BACKEND,
    show_default=True,
    help=_LOOP_BACKEND_HELP,
)
def converge_config(context_path: Path, loop_backend: str) -> None:
    """Print the review convergence settings for this session (#2787).

    Prints {convergence, max_cycles, confirm_backend, confirm_backend_configured,
    confirm_not_independent, loop_backend, source, ticket_id, dispatch_refusal}.
    `confirm_backend` is the resolved backend. `convergence` is `clean` whenever
    the context's `review.convergence` says so (source `context`). A missing,
    unreadable, malformed or below-schema-15 context carries no review object,
    so it reads as `blocking` with source `no_review_in_context`: this command
    never exits non-zero for a context shape, so a non-zero exit is always a cw
    fault.

    `dispatch_refusal` is non-null only for a clean client whose context has no
    usable ticket id (an interactive `cw start` session, or `cw spawn
    --headless`): clean-mode review needs a ticket dispatched through
    `cw dev-queue`. The run-start guards read it; this command never refuses.
    """
    emit_json(_config_report(load_convergence_config(context_path), loop_backend))


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def _history_rewritten(
    existing: ConvergenceLedger | None, worktree: Path, head_sha: str
) -> bool:
    """True when the last recorded round's sha is no longer an ancestor of HEAD.

    ``git merge-base --is-ancestor`` exits 1 for "not an ancestor" and 128 for
    an unknown commit; both mean the branch history moved under the ledger, so
    it cannot be trusted (more review, never a skipped one).
    """
    if existing is None or not existing.rounds:
        return False
    last_sha = existing.rounds[-1].reviewed_sha
    try:
        result = run_git(
            ["merge-base", "--is-ancestor", last_sha, head_sha],
            cwd=worktree,
            capture_output=True,
        )
    except OSError as exc:
        msg = f"git merge-base could not run in {worktree}: {exc}"
        raise CwError(msg) from exc
    return result.returncode != 0


def _init_report(reconciled: Reconciled) -> dict[str, object]:
    ledger = reconciled.ledger
    report: dict[str, object] = {
        "convergence": "clean",
        "status": ledger.status,
        "max_cycles": ledger.max_cycles,
        "confirm_backend": ledger.confirm_backend,
        "confirm_not_independent": ledger.confirm_not_independent,
        "reset_reason": reconciled.reset_reason,
    }
    if reconciled.released is not None:
        report["released"] = reconciled.released.model_dump(mode="json")
    if ledger.status == "stopped":
        report["stop"] = stop_summary(ledger)
        report["reemitted"] = True
    return report


@converge.command(name="init")
@click.option("--ticket-id", required=True, help="The ticket this review belongs to.")
@click.option("--fork-point", required=True, help="The branch's fork point sha.")
@click.option(
    "--reviewed-sha",
    required=True,
    help="HEAD at the start of this session; also the Checkpoint 3a sha.",
)
@click.option(
    "--no-confirm",
    is_flag=True,
    default=False,
    help="Skip the independent confirm round (/prep-pr only).",
)
@click.option(
    "--context",
    "context_path",
    default=HOOK_CONTEXT_RELATIVE_PATH,
    show_default=True,
    type=click.Path(path_type=Path),
    help=_CONTEXT_HELP,
)
@click.option(
    "--loop-backend",
    default=_DEFAULT_LOOP_BACKEND,
    show_default=True,
    help=_LOOP_BACKEND_HELP,
)
@worktree_option
@ledger_option
@handle_errors
def converge_init(
    ticket_id: str,
    fork_point: str,
    reviewed_sha: str,
    no_confirm: bool,
    context_path: Path,
    loop_backend: str,
    worktree: Path,
    ledger_rel: Path,
) -> None:
    """Create or reconcile the convergence ledger, idempotently (#2787).

    A no-op in blocking mode. In clean mode it reads the review settings itself
    (the caller passes no backend) and reconciles the ledger at the start of
    every REVIEW session: a ledger for another ticket or fork point, or whose
    last round is no longer an ancestor of HEAD, is discarded and restarted
    (`reset_reason`); `max_cycles` is refreshed from the current config; a
    converged ledger whose HEAD moved reopens; a stop on a new tip reopens as a
    new epoch; and a stop on the same tip is released only when it is
    operator-resolvable (a raised cap, an operator-action checklist, or every
    recurring finding operator-settled). Any other stop is re-emitted unchanged
    (`stop`, `reemitted: true`).

    --no-confirm is for /prep-pr only: the ledger records no confirm round.
    """
    config = load_convergence_config(context_path)
    if config.convergence != "clean":
        emit_json({"convergence": "blocking", "status": "noop"})
        return
    path = worktree / ledger_rel
    existing = load_ledger_or_none(path)
    confirm = (
        ResolvedBackend("none", False)
        if no_confirm
        else resolve_confirm_backend(config.confirm_backend_configured, loop_backend)
    )
    fresh = new_ledger(
        ticket_id=ticket_id,
        fork_point=fork_point,
        checkpoint_3a_sha=reviewed_sha,
        max_cycles=config.max_cycles,
        loop_backend=loop_backend,
        confirm=confirm,
    )
    reconciled = reconcile_ledger_for_init(
        existing,
        fresh=fresh,
        ticket_id=ticket_id,
        fork_point=fork_point,
        history_rewritten=_history_rewritten(existing, worktree, reviewed_sha),
        head_sha=reviewed_sha,
        max_cycles=config.max_cycles,
        now=_utc_now_iso(),
    )
    if reconciled.ledger != existing:
        save_ledger(path, reconciled.ledger)
    emit_json(_init_report(reconciled))


# ---------------------------------------------------------------------------
# suppress
# ---------------------------------------------------------------------------


class _SuppressInput(BaseModel):
    """The ``suppress`` request: a verdict or a findings list, exactly one."""

    model_config = ConfigDict(extra="forbid")

    verdict: ReviewVerdict | None = None
    findings: list[RoundFinding] | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> _SuppressInput:
        if (self.verdict is None) == (self.findings is None):
            msg = "exactly one of 'verdict' or 'findings' is required"
            raise ValueError(msg)
        return self


@converge.command(name="suppress")
@click.argument("path")
@ledger_option
@handle_errors
def converge_suppress(path: str, ledger_rel: Path) -> None:
    """Stamp findings the ledger already settled, before adjudication (#2787).

    PATH is a file or '-'. Payload: {"verdict": <ReviewVerdict>} or
    {"findings": [<round finding>, ...]}, exactly one key.

    A resurfaced settled finding (settled by adjudication or by an operator
    `operator-settle`) is STAMPED, not removed: it keeps its place with outcome
    reject or defer and the stored rationale. In the verdict form `must_fix`
    and `blocking` are recomputed from the unstamped remainder; the frozen
    counts, `rejected_must_fix` and `run_failures_with_should_fix_discards` are
    untouched.

    Prints {"verdict"|"findings": ..., "adjudications": [...], "suppressed":
    <count>}. Append the adjudications verbatim to your ADJUDICATIONS array.
    """
    payload = _parse_payload_or_exit(path, _SuppressInput)
    ledger = require_ledger(ledger_rel)
    body: dict[str, object] = {}
    if payload.verdict is not None:
        verdict, adjudications = suppress_settled(ledger, payload.verdict)
        body["verdict"] = json.loads(verdict.model_dump_json())
    else:
        findings, adjudications = suppress_settled(ledger, payload.findings or [])
        body["findings"] = [f.model_dump(mode="json") for f in findings]
    body["adjudications"] = [a.model_dump(mode="json") for a in adjudications]
    body["suppressed"] = len(adjudications)
    emit_json(body)


# ---------------------------------------------------------------------------
# record-round
# ---------------------------------------------------------------------------


def _write_out_verdict(out: Path, verdict: ReviewVerdict) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out, verdict.model_dump_json(indent=2) + "\n")


@converge.command(name="record-round")
@click.argument("path")
@click.option(
    "--out-verdict",
    default=None,
    type=click.Path(path_type=Path, dir_okay=False),
    help="Also write the round's verdict here (in addition to the ledger dir).",
)
@ledger_option
@handle_errors
def converge_record_round(
    path: str, out_verdict: Path | None, ledger_rel: Path
) -> None:
    """Record one review round and print the decision (#2787).

    PATH is a file or '-'. Payload: {kind: loop|confirm, backend, reviewed_sha,
    roster, and exactly one of verdict (a ReviewVerdict) or findings (a list of
    round findings)}. Prints the round report; obey its `decision`.

    A verdict payload is read by the engine: a would-be-clean round whose
    verdict carries a mechanically rejected MUST_FIX or a reviewer failure that
    discarded findings is refused (`unread_findings:`), and so is one with a
    role that failed outright (`failed_role:`). Any refusal, a confirm round at
    the wrong sha or backend, an undecided finding or an unreadable ledger
    exits 1 with `Error: ...` and records nothing: no ledger write, no
    round-verdict file, no --out-verdict file.

    A verdict payload also persists the verdict as round-<n>-verdict.json
    beside the ledger, which each fix cycle's verify-fixes reads back.
    """
    payload = _parse_payload_or_exit(path, RoundInput)
    ledger = require_ledger(ledger_rel)
    findings, unread = round_input_findings(payload)
    frozen = (
        None if payload.verdict is None else frozen_counts_from_verdict(payload.verdict)
    )
    updated, report = record_round(
        ledger,
        kind=payload.kind,
        backend=payload.backend,
        reviewed_sha=payload.reviewed_sha,
        findings=findings,
        roster=payload.roster,
        now=_utc_now_iso(),
        unread=unread,
        frozen=frozen,
    )
    if payload.verdict is not None:
        save_round_verdict(ledger_rel, report.round, payload.verdict)
        if out_verdict is not None:
            _write_out_verdict(out_verdict, payload.verdict)
    save_ledger(ledger_rel, updated)
    click.echo(report.model_dump_json(indent=2))


# ---------------------------------------------------------------------------
# confirm-failed
# ---------------------------------------------------------------------------


@converge.command(name="confirm-failed")
@click.option(
    "--cause",
    required=True,
    help="Why the confirm backend failed (binary_missing, backend_error, crash, ...).",
)
@click.option("--detail", default="", help="What the backend reported.")
@click.option(
    "--client",
    "-c",
    default=None,
    help="Client name, only to fill the requeue hint in the blocker.",
)
@ledger_option
@handle_errors
def converge_confirm_failed(
    cause: str, detail: str, client: str | None, ledger_rel: Path
) -> None:
    """Record a confirm-backend outage and print the exit to take (#2787).

    Prints {"decision": "stop_confirm_unavailable" | "stop_confirm_escalated",
    "consecutive": <n>, "blocker": <sentinel blocker>, "review": <sentinel
    review fragment>}. `stop_confirm_unavailable` is the uncharged
    `confirm_backend_unavailable` park. The fourth same-cause failure at one
    sha with no round recorded between them is `stop_confirm_escalated`, an
    `agent_block` outside the uncharged class. The cap lives in the engine, so
    obey `decision`.

    Adds no round and changes no counter: the ledger stays awaiting_confirm at
    the same sha. Only a backend-down cause belongs here; a content-level round
    failure on a healthy backend records nothing.
    """
    ledger = require_ledger(ledger_rel)
    updated, report = record_confirm_failure(
        ledger, cause=cause, detail=detail, now=_utc_now_iso()
    )
    escalated = report.decision == "stop_confirm_escalated"
    save_ledger(ledger_rel, updated)
    emit_json(
        {
            "decision": report.decision,
            "consecutive": consecutive_confirm_failures(updated, cause=cause),
            "blocker": confirm_blocker_fragment(
                updated, escalated=escalated, client=client
            ),
            "review": sentinel_review_fragment(updated),
        }
    )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@converge.command(name="status")
@worktree_option
@ledger_option
@handle_errors
def converge_status(worktree: Path, ledger_rel: Path) -> None:
    """Print where the convergence ledger stands and what to do next (#2787).

    Prints status, next_action (loop_round, confirm, fix, done, stopped),
    checkpoint_3a_sha, the frozen cycle-0 counts, fix_cycles_used,
    next_fix_cycle, max_cycles, roster, awaiting_confirm_sha, last_clean_sha,
    `stop` ({reason, identities, settle_payload, releasable, release_hint},
    null unless stopped), releases and sentinel_review. A resumed session obeys
    `next_action` rather than assuming where it left off.
    """
    ledger = require_ledger(worktree / ledger_rel)
    head_sha = git_stdout(["rev-parse", "HEAD"], worktree)
    emit_json(status_summary(ledger, head_sha=head_sha))


# ---------------------------------------------------------------------------
# history-comment
# ---------------------------------------------------------------------------


@converge.command(name="history-comment")
@click.option(
    "--out",
    "out",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
    help="Where to write the '## Review Convergence History' comment.",
)
@ledger_option
@handle_errors
def converge_history_comment(out: Path, ledger_rel: Path) -> None:
    """Write the convergence history tracker comment (#2787).

    Writes the file only when the ledger holds rounds, and prints
    {"written": <bool>, "out": <path>}. A stopped ledger's comment also names
    the stop, what releases it and a ready-to-paste `operator-settle` payload.
    """
    ledger = load_ledger_or_none(ledger_rel)
    written = ledger is not None and bool(ledger.rounds)
    if ledger is not None and written:
        stop_reason = ledger.stop_reason if ledger.status == "stopped" else None
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(out, render_history_comment(ledger, stop_reason))
    emit_json({"written": written, "out": str(out)})
