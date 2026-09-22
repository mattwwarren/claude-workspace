"""The ``cw review`` commands without a seam of their own (#2048).

``register``, ``adjudicate``, ``check-voided``, ``settle`` and
``verify-fixes`` — the commands left after ``consolidate`` and the
diff-integrity guards were given their own submodules.

``cw review register <pr-url>`` records a PR you were asked to review as a
watched PR (``DevQueueStore.watched_prs``). No ``list``/``remove`` subcommand
exists this slice (R11) — operators inspect ``dev_queue.json`` directly until a
later slice adds them.

``cw review adjudicate <path>`` and ``cw review verify-fixes <path>`` (#1805)
are the two steps after ``cw review consolidate``: the first stamps the
session's own FIX / REJECT / DEFER decisions into the verdict (and renders the
matching ``.cw/deferred-findings.md``), the second downgrades any ``"fixed"``
disposition the fix-cycle diff does not substantiate. Adjudication stays a
judgment call made by the coordinating session — these commands only make its
outcome machine-readable instead of re-typed into two places.

``cw review check-voided <path>`` (#1814) runs between consolidate and
adjudicate: it suppresses findings a prior pass's operator decision already
settled, and renders the durable record of those decisions back out for
posting to the ticket. It is the Claude-native half of a mechanism the codex
backend reaches through ``cw.codex_review`` instead — same library function,
same outcome, no coordinating session required on that side.

``cw review settle <path>`` (#2210) is the cross-round adjudication ledger's
first production writer. #1838 shipped that ledger's renderer, parser and
mechanical backstop but nothing that ever CALLED the renderer, so the runbook
told operators to compute the key by hand from a ``python -c`` one-liner. This
turns a payload — the one every blocking codex review comment now prints — into
the postable ``REVIEW-FINDING-DISPOSITIONS`` marker.

``settle`` is the only command here that mints a durable, blocking
SUPPRESSION rather than recording one pass's outcome, so it carries an audit
contract the others do not: a mandatory ``--reason``, provenance
(actor / CLI-stamped UTC timestamp / verbatim summary / reviewed sha) on every
record, one ``review.finding_settled`` event per settled finding, and a flat
refusal to run anywhere that is not provably an interactive session — a
dispatch worker, or a directory whose dispatch context cannot be resolved at
all. See ADR-0016.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import click
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cw.atomic import atomic_write_text
from cw.cli._base import handle_errors
from cw.cli.review._diff_integrity import (
    _NO_BASE_CHECK_HELP,
    _require_base_xor_no_base_check,
    _run_base_check_if_requested,
)
from cw.cli.review._group import (
    _build_captured_diff,
    _parse_payload_or_exit,
    review,
)
from cw.exceptions import CwError
from cw.review_adjudication import (
    REJECTED_ENTRY_SEVERITY,
    Adjudication,
    VoidedFinding,
    apply_adjudication,
    apply_voided_suppression,
    matched_adjudications,
    merge_deferred_adjudications,
    parse_deferred_findings_md,
    parse_voided_findings_block,
    render_deferred_findings_md,
    render_voided_findings_block,
    verify_fixed_dispositions,
)
from cw.review_finding_dispositions import (
    FindingDisposition,
    Outcome,
    build_finding_disposition_ledger,
    disposition_event_payload,
    disposition_event_type,
    render_finding_disposition_block,
    split_disposition_key,
)
from cw.review_findings import ReviewVerdict


@review.command(name="register")
@click.argument("pr_url")
@handle_errors
def review_register(pr_url: str) -> None:
    """Register a PR you were asked to review as a watched PR.

    Parses the GitHub PR URL, resolves your gh identity, reads the PR's live
    ``reviewRequests``, and records a watched PR when you are individually (not
    team-) requested. Prints the outcome reason and exits 0 for a non-error
    "not registered" case (team-targeted, not-you, already-registered); exits
    non-zero only when your identity cannot be resolved, the URL is
    unparseable, or the PR cannot be fetched.

    Your GitHub identity resolves via the same precedence
    ``resolve_operator_login_for_repo`` uses everywhere else: the PR's repo
    in ``orchestrator.yaml``'s ``operator_github_login_by_repo`` map wins when
    set, otherwise your process gh identity (RFC 0011 follow-up #1171).
    """
    from cw.config import load_orchestrator_config
    from cw.gh import fetch_pr_view
    from cw.operator_identity import cached_gh_login, resolve_operator_login_for_repo
    from cw.pr_hydrate import (
        _parse_pr_url,
        resolve_and_register_review_request,
    )

    parsed = _parse_pr_url(pr_url)
    if parsed is None:
        msg = f"Could not parse a GitHub PR URL from: {pr_url!r}"
        raise CwError(msg)
    repo, pr_number = parsed

    config = load_orchestrator_config()
    operator_login = resolve_operator_login_for_repo(
        repo, config, fallback=cached_gh_login()
    )
    if operator_login is None:
        msg = (
            "Could not resolve your GitHub identity (gh api user failed)."
            " Ensure gh is installed and authenticated (gh auth status)."
        )
        raise CwError(msg)

    data = fetch_pr_view(pr_url)
    if data is None:
        msg = f"Could not fetch PR view for {pr_url} (gh pr view failed)."
        raise CwError(msg)
    review_requests = data.get("reviewRequests")
    reviewer_nodes = review_requests if isinstance(review_requests, list) else []

    registered, reason = resolve_and_register_review_request(
        repo=repo,
        pr_number=pr_number,
        pr_url=pr_url,
        reviewer_nodes=reviewer_nodes,
        operator_login=operator_login,
        source="cli",
        requester_login=None,
    )
    if registered:
        click.echo(f"Registered watched PR {repo}#{pr_number}.")
    else:
        click.echo(f"Not registered ({reason}).")


class _AdjudicateInput(BaseModel):
    """Request envelope for ``cw review adjudicate`` (#1805).

    The verdict is the one ``cw review consolidate`` printed at Checkpoint 3a;
    the adjudications are one entry per finding the coordinating session
    bucket-sorted. Same envelope shape as :class:`_ConsolidateInput` — owned by
    this CLI module, not by the library it calls.
    """

    verdict: ReviewVerdict
    adjudications: list[Adjudication] = Field(default_factory=list)


@review.command(name="adjudicate")
@click.argument("path")
@click.option(
    "--deferred-findings-out",
    default=None,
    type=click.Path(path_type=Path),
    help=(
        "Also render the rejected/deferred adjudications to this path "
        "(the .cw/deferred-findings.md artifact Stage 4 Step 4d "
        "consumes), merging them with any prior content already at "
        "this path rather than overwriting it. Each newly-applied "
        "entry is stamped with a round number and a recorded_at "
        "timestamp; a pre-#1840 legacy-shaped file (no round/date "
        "stamps) is read and merged without error. Nothing is "
        "written when there is nothing to record — every finding "
        "was fixed and no prior content exists to preserve."
    ),
)
@handle_errors
def review_adjudicate(path: str, deferred_findings_out: Path | None) -> None:
    """Stamp adjudication outcomes into a ReviewVerdict (#1805).

    PATH is a file path or '-' for stdin. Payload: {"verdict": <the
    ReviewVerdict from `cw review consolidate`>, "adjudications": [{"severity":
    ..., "file": ..., "line_start": ..., "line_end": ..., "evidence": ...,
    "summary": ..., "outcome": "fix|reject|defer", "rationale": ...}]}.

    Each accepted finding is stamped from its matching adjudication entry;
    a finding no entry covers is stamped "dropped", and blocking/must_fix/
    review.deferred are recomputed from the stamped result. An entry matching
    no finding never fails the command — it is counted in the printed
    verdict's `unmatched_adjudication_count` so the approval gate can see it,
    and is excluded from the rendered `--deferred-findings-out` artifact (an
    entry nobody's disposition reflects must not appear there as if it did).

    When --deferred-findings-out is given, any prior content already at
    that path is read back and merged with this round's rejected/deferred
    entries rather than overwritten. Entries dedupe by content fingerprint
    (severity, file, line_start, line_end, evidence, summary, outcome,
    rationale) — excluding `round`/`recorded_at` — so an identical
    re-adjudication collapses to one entry while a genuine outcome flip
    (e.g. REJECT then later DEFER for the same finding) accumulates as
    two. Only entries newly applied by this call are stamped with a
    `round` number and a `recorded_at` timestamp; entries already present
    in the prior file — including ones written before this stamping
    existed — are carried through unchanged. Content matching neither the
    current nor the pre-#1840 shape is a hard failure (CwError); an
    absent or empty prior file is simply "nothing to merge".

    On success: exits 0, prints the stamped ReviewVerdict as JSON to stdout.
    On failure: exits 1, prints 'field.path: message' lines to stderr —
    except a malformed --deferred-findings-out prior file, which exits 1
    with a plain CwError message instead of the field.path format.
    """
    parsed = _parse_payload_or_exit(path, _AdjudicateInput)
    verdict = apply_adjudication(parsed.verdict, parsed.adjudications)

    if deferred_findings_out is not None:
        applied = matched_adjudications(parsed.verdict.accepted, parsed.adjudications)
        _write_deferred_findings(deferred_findings_out, applied)

    click.echo(verdict.model_dump_json(indent=2))


def _read_prior_deferred(path: Path) -> list[Adjudication]:
    """The entries already recorded at *path*, or ``[]`` when there are none.

    A parse failure is fatal rather than a silent restart from empty: the
    alternative is overwriting durable prior records with this round's alone,
    which is #1840's own bug wearing a different hat.
    """
    if not path.exists():
        return []
    try:
        return parse_deferred_findings_md(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        msg = (
            f"Could not parse the existing --deferred-findings-out file at "
            f"{path}: {exc}. Refusing to overwrite it — inspect or remove it "
            "by hand, then re-run."
        )
        raise CwError(msg) from exc


def _artifact_entry(entry: Adjudication, next_round: int, now: str) -> Adjudication:
    """*entry* stamped with this round's context, in the artifact's field space.

    Two things happen here, both required for the merge to behave:

    - **Stamping.** ``round``/``recorded_at`` are filled in exactly the way
      :func:`_stamp_voided_at` fills ``voided_at``: the coordinating session
      supplies the judgment, the CLI supplies the clock, and a value already
      present is never overwritten.
    - **Projection.** ``line_start``/``line_end``/``evidence`` (and, for a
      rejected entry, ``severity``) are reduced to what the rendered artifact
      actually records. The artifact has never carried them, so an entry read
      back from a prior round cannot have them either; leaving them on this
      round's entries would make an identical re-adjudication miss its own
      prior record and duplicate it. Nothing observable is lost — every
      dropped field is one :func:`render_deferred_findings_md` never writes.
    """
    update: dict[str, object] = {
        "line_start": None,
        "line_end": None,
        "evidence": "",
    }
    if entry.outcome == "reject":
        update["severity"] = REJECTED_ENTRY_SEVERITY
    if entry.round is None:
        update["round"] = next_round
    if not entry.recorded_at.strip():
        update["recorded_at"] = now
    return entry.model_copy(update=update)


def _write_deferred_findings(path: Path, applied: list[Adjudication]) -> None:
    """Merge *applied* into the artifact at *path* and re-render it (#1840)."""
    prior = _read_prior_deferred(path)
    next_round = max((e.round for e in prior if e.round is not None), default=0) + 1
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamped = [_artifact_entry(entry, next_round, now) for entry in applied]
    rendered = render_deferred_findings_md(merge_deferred_adjudications(prior, stamped))
    # "" means there is nothing to record at all — every finding was fixed and
    # no prior content exists to preserve. The documented rule is to omit the
    # file entirely rather than leave an empty artifact behind.
    if rendered:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, rendered)


class _CheckVoidedInput(BaseModel):
    """Request envelope for ``cw review check-voided`` (#1814).

    ``comment_bodies`` are the live-fetched ticket comments the coordinating
    session already holds (mandatory per #1730's "comments are live, not
    cached" rule) — every prior pass's voided-findings sentinel is parsed back
    out of them. ``new_voided_entries`` are the ones this pass just settled at
    Checkpoint 3a step 4c.

    ``ticket_id`` is required because it is this path's ``correlation_id``
    source: the codex path reads it off its ``TicketTask``, and there is no
    equivalent object here, so it has to come in on the payload rather than
    leaving the mandatory suppression event uncorrelated.
    """

    verdict: ReviewVerdict
    ticket_id: str
    comment_bodies: list[str] = Field(default_factory=list)
    new_voided_entries: list[VoidedFinding] = Field(default_factory=list)


class _CheckVoidedOutput(BaseModel):
    """Response envelope for ``cw review check-voided`` (#1814).

    Two values, not one: the suppressed verdict continues Checkpoint 3a, and
    the adjudications are appended verbatim to the session's ``ADJUDICATIONS``
    array so the later ``cw review adjudicate`` pass re-stamps the same outcome
    from the same single source of truth.
    """

    verdict: ReviewVerdict
    adjudications: list[Adjudication]


def _utc_now_iso() -> str:
    """The ISO-8601 stamp both record-minting commands write (#2210).

    One implementation, so ``check-voided``'s ``voided_at`` and ``settle``'s
    ``recorded_at`` cannot come out in two shapes.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp_voided_at(entry: VoidedFinding) -> VoidedFinding:
    """Fill a blank ``voided_at`` with now, leaving a supplied one alone.

    The coordinating session supplies the judgment; the CLI supplies the
    clock. Re-stamping an entry that already carries a date would rewrite
    history on every idempotent re-post.
    """
    if entry.voided_at.strip():
        return entry
    return entry.model_copy(update={"voided_at": _utc_now_iso()})


@review.command(name="check-voided")
@click.argument("path")
@click.option(
    "--voided-findings-out",
    default=None,
    type=click.Path(path_type=Path),
    help=(
        "Also render the merged voided-findings record to this path, as a "
        "postable '## Voided Review Findings' ticket comment. Nothing is "
        "written when there is no void to record."
    ),
)
@handle_errors
def review_check_voided(path: str, voided_findings_out: Path | None) -> None:
    """Suppress findings an operator already voided on a prior pass (#1814).

    PATH is a file path or '-' for stdin. Payload: {"verdict": <the
    ReviewVerdict from `cw review consolidate`>, "ticket_id": "<id>",
    "comment_bodies": ["<live-fetched ticket comment>", ...],
    "new_voided_entries": [{"severity": ..., "file": ..., "summary": ...,
    "evidence": ..., "operator_comment_id": ..., "operator_comment_excerpt":
    ..., "original_rationale": ...}]}.

    A finding is suppressed only when its content anchor — severity, file,
    summary, and evidence — matches a recorded void exactly. File and line
    position are deliberately NOT the identity: a voided finding whose code
    moved still matches, and a genuinely new finding at the voided one's old
    line never does.

    Each suppression stamps `disposition="rejected"`, drops the finding from
    `must_fix`/`blocking`, and emits one `review.finding_voided` event
    correlated to `ticket_id`.

    On success: exits 0, prints {"verdict": ..., "adjudications": [...]} to
    stdout. Append the adjudications verbatim to your ADJUDICATIONS array.
    On failure: exits 1, prints 'field.path: message' lines to stderr.
    """
    parsed = _parse_payload_or_exit(path, _CheckVoidedInput)
    merged = [
        *parse_voided_findings_block(parsed.comment_bodies),
        *(_stamp_voided_at(entry) for entry in parsed.new_voided_entries),
    ]
    verdict, adjudications = apply_voided_suppression(
        parsed.verdict, merged, ticket_id=parsed.ticket_id
    )

    if voided_findings_out is not None:
        rendered = render_voided_findings_block(merged)
        # "" means there is nothing to record — omit the artifact entirely
        # rather than leave an empty one behind, same rule as
        # --deferred-findings-out.
        if rendered:
            voided_findings_out.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(voided_findings_out, rendered)

    output = _CheckVoidedOutput(verdict=verdict, adjudications=adjudications)
    click.echo(output.model_dump_json(indent=2))


class _SettleEntry(BaseModel):
    """One operator decision to record in the ledger (#2210).

    ``file`` and ``summary`` are the finding's identity, copied VERBATIM from
    the blocking review comment's payload — normalization happens on ingest via
    ``fingerprint_v1``, so a hand-normalized value here would key differently
    from the re-derived finding it is meant to suppress.

    ``file="N/A"`` is rejected rather than dropped: that is #1817's
    no-diff-anchor case, which cannot be keyed at all, and silently omitting it
    from the marker would leave an operator believing they settled something.

    ``reviewed_sha`` is the commit the finding was raised against, so the
    record can answer "against what code was this silenced". The rendered
    payload fills it from the verdict; a hand-written payload may leave it
    blank and pass ``--reviewed-sha`` instead. There is deliberately no
    fallback to ``git rev-parse HEAD``: the sha of whatever directory the
    operator happened to be standing in is not evidence.

    ``extra="forbid"`` so a stale or mistyped key fails loudly rather than
    being dropped. In particular ``recorded_at`` is NOT accepted: it is audit
    data, stamped only by this command's own UTC clock.
    """

    model_config = ConfigDict(extra="forbid")

    file: str
    summary: str
    outcome: Outcome
    rationale: str = ""
    reviewed_sha: str = ""

    @field_validator("file")
    @classmethod
    def _keyable_file(cls, value: str) -> str:
        if not value.strip():
            msg = "file must be non-empty"
            raise ValueError(msg)
        if value == "N/A":
            msg = "file 'N/A' has no path to key on; this finding cannot be settled"
            raise ValueError(msg)
        return value

    @field_validator("summary")
    @classmethod
    def _summary_nonempty(cls, value: str) -> str:
        if not value.strip():
            msg = "summary must be non-empty"
            raise ValueError(msg)
        return value


class _SettleInput(BaseModel):
    """Request envelope for ``cw review settle`` (#2210)."""

    model_config = ConfigDict(extra="forbid")

    entries: list[_SettleEntry] = Field(min_length=1)


_SETTLE_IN_WORKER_MSG = (
    "Refusing to settle a review finding from inside a dispatch worker "
    "(.claude/cw-context.json reports headless). A ledger entry silently "
    "suppresses every future re-raise of that finding, so it must be "
    "minted by an operator on their own machine. Save the payload, and "
    "run `cw review settle` there."
)

_SETTLE_CONTEXT_UNRESOLVED_MSG = (
    "Refusing to settle a review finding: this directory's dispatch context "
    "could not be resolved, so there is nothing that says you are NOT inside "
    "a worker. No `.claude/cw-context.json` was found in this directory or "
    "any parent, or the one that was found is unreadable, is not a JSON "
    "object, or carries no boolean `headless` field. Run `cw review settle` "
    "from an interactive cw session worktree (or any directory beneath one) — "
    "a directory whose `.claude/cw-context.json` reports `headless: false`. A "
    "plain checkout of the repo has no such file. Nothing was written: no "
    "marker, no --out file, no event."
)


def _refuse_settle_outside_an_interactive_session() -> None:
    """Refuse to mint a suppression unless this is provably an operator (#2210).

    A settle is the one act that can silence a real defect permanently and
    invisibly. A worker settling findings raised by its own reviewer is the
    pipeline adjudicating itself — the exact self-suppression this ticket
    exists to guard against — so it is refused outright. There is no bypass
    flag, env var or option: a control an agent can switch off is not a
    control.

    Keyed on the nearest ``.claude/cw-context.json`` (searched upward from cwd,
    the way ``check_not_main_checkout.py`` does) reporting ``headless``, which
    is what ``cw`` stamps for every session it spawns — ``True`` for a
    DAEMON-origin ``/auto-dev`` worker, an explicit ``False`` for an
    interactive one.

    **Fails CLOSED** (#2210 round 4). The only state that proceeds is a
    discovered context whose ``headless`` is the JSON boolean ``false``.
    Everything else — no context file anywhere above cwd, an unreadable or
    malformed one, one with no ``headless`` key, one whose ``headless`` is not
    a bool — refuses. :func:`~cw.cli._hook_io.find_cw_context` cannot tell
    "there is no dispatch context here" apart from "the dispatch context could
    not be read", and an indeterminate answer used to read as "operator's own
    machine": a worker whose context file was missing, truncated or written by
    a future schema would have settled its own reviewer's findings. The guard
    that decides whether a durable suppression may be minted is the wrong
    place to be optimistic — same posture, for the same reason, as #2213.

    The operational cost is that a settle must be run from inside a cw session
    worktree rather than a plain checkout; the runbook says so.
    """
    from cw.cli._hook_io import find_cw_context

    context = find_cw_context(Path.cwd())
    headless = None if context is None else context.get("headless")
    if headless is False:
        return
    raise CwError(
        _SETTLE_IN_WORKER_MSG if headless is True else _SETTLE_CONTEXT_UNRESOLVED_MSG
    )


def _resolve_settle_actor() -> str:
    """The gh login recorded as having settled these findings (#2210).

    Same resolver ``cw review register`` uses. Refusing when it cannot be
    resolved is the point: an anonymous suppression record answers none of the
    three questions an audit record exists to answer.
    """
    from cw.operator_identity import cached_gh_login

    actor = cached_gh_login()
    if actor is None:
        msg = (
            "Could not resolve your GitHub identity (gh api user failed), so "
            "this settle would be recorded anonymously. Ensure gh is installed"
            " and authenticated (gh auth status)."
        )
        raise CwError(msg)
    return actor


def _settle_reviewed_sha(entry: _SettleEntry, fallback: str) -> str:
    """The reviewed sha for *entry*, preferring the payload's own."""
    resolved = entry.reviewed_sha.strip() or fallback
    if not resolved:
        msg = (
            f"entry for {entry.file!r} has no reviewed_sha and none was given: "
            "pass --reviewed-sha <sha> (the commit the finding was raised "
            "against). A settle that cannot say which code it silenced is not "
            "an audit record."
        )
        raise CwError(msg)
    return resolved


def _emit_settle_events(
    ledger: dict[str, FindingDisposition], ticket: str | None
) -> None:
    """One audit event per settled finding (#2210, #2232).

    The mirror of ``review.finding_disposition_suppressed``: that event records
    a suppression firing, this one records it being created. Emitted over the
    COLLAPSED ledger, so two payload entries that key alike are one settled
    finding and one event — the same arithmetic the marker itself uses.

    The event TYPE carries the semantic, not a field inside the payload — see
    :func:`~cw.review_finding_dispositions.disposition_event_type`, which
    makes that choice for every emitter. The payload is identical either way;
    it already carries ``outcome``, so nothing is lost to a consumer that
    wants both. Both the type choice and the payload shape moved to the ledger
    module (#2232) once the review pass's comment-thread sync became a second
    emitter of the same events: they now cannot drift apart, and neither site
    carries a raw ``"REVERSED"`` literal.

    **Raises rather than degrading** (#2210 round 2). ``record_event`` is file
    I/O and can fail; the caller must not write a marker for a finding whose
    audit did not record, so an emit failure aborts the whole settle with a
    message naming the finding and the failure. ALL events are emitted before
    the caller writes anything, so a failure part-way through leaves the
    earlier findings with an audit record and no effect — see the ordering
    comment at the call site for why that asymmetry is the right one.
    """
    from cw.events import record_event

    for key, entry in sorted(ledger.items()):
        file = split_disposition_key(key)[0]
        event_type = disposition_event_type(entry)
        try:
            record_event(
                event_type,
                payload=disposition_event_payload(key, entry),
                correlation_id=ticket,
            )
        except OSError as exc:
            msg = (
                f"Could not record the {event_type.value} audit event for "
                f"{file} ({entry.summary!r}): {exc}. Nothing was written — no "
                "marker, no --out file. A suppression with no audit record is "
                "invisible, so the settle is refused rather than recorded "
                "half-way. Fix the event store and re-run the same payload."
            )
            raise CwError(msg) from exc


@review.command(name="settle")
@click.argument("path")
@click.option(
    "--reason",
    required=True,
    type=str,
    help=(
        "Why these findings are settled. Required, and recorded on every "
        "entry the payload does not give its own `rationale`."
    ),
)
@click.option(
    "--reviewed-sha",
    default="",
    type=str,
    help=(
        "The commit the findings were raised against, for entries whose "
        "payload does not carry `reviewed_sha` (hand-written payloads). "
        "Never inferred from the current checkout."
    ),
)
@click.option(
    "--ticket",
    default=None,
    type=str,
    help="Ticket id to correlate the settle/reversal audit events to.",
)
@click.option(
    "--out",
    default=None,
    type=click.Path(path_type=Path),
    help=(
        "Also write the rendered marker to this path, ready to post with "
        "`gh issue comment --body-file`."
    ),
)
@handle_errors
def review_settle(
    path: str,
    reason: str,
    reviewed_sha: str,
    ticket: str | None,
    out: Path | None,
) -> None:
    """Record operator-settled findings as a postable marker (#2210).

    Run this on YOUR OWN MACHINE, from an interactive cw session worktree (or
    any directory beneath one). It refuses inside a dispatch worker — a ledger
    entry silently suppresses every future re-raise of that finding, so the
    pipeline must not be able to settle its own reviewer's findings — and it
    refuses just as flatly when it cannot tell: the nearest
    `.claude/cw-context.json` must be readable and report `headless: false`.
    A plain checkout of the repo has no such file, so run it from a session
    worktree. There is no bypass flag.

    PATH is a file path or '-' for stdin. Payload: {"entries": [{"file":
    "<path>", "summary": "<verbatim finding summary>", "outcome":
    "REJECTED"|"ACCEPTED"|"REVERSED", "rationale": "<why, optional>",
    "reviewed_sha": "<sha>"}]}. Every blocking codex review comment prints one
    such payload per finding under "### Settle a finding" — paste it unedited.

    ROLLBACK (#2232): `outcome: "REVERSED"` withdraws a settle you already
    made. Paste the SAME `file` and `summary` the earlier settle used — read
    them off `cw review dispositions <ticket> --json` (its `summary` field is
    always the full verbatim text) or the original settle marker; the human
    table truncates long summaries for scanning and is not a payload source —
    and post the marker this prints. The newest-`recorded_at`-wins merge is
    what makes the withdrawal stick, so no new command and no new write path
    is involved; a reversed record matches neither suppression tier and is not
    shown to the reviewer as a decision. It stays visible in `cw review
    dispositions`, because reversal history is part of the audit trail.

    --reason is REQUIRED and must be non-blank; it is the rationale recorded
    for every entry that does not carry its own. An entry's own `rationale`
    wins when set, so several findings can be settled for different reasons in
    one call.

    Identity is the VERBATIM `file` and `summary`; the same normalizer the
    reviewer's re-raise will hit is applied on ingest, so nothing needs
    hand-normalizing. Two entries that key alike collapse, the later one in the
    payload winning.

    Every record carries provenance: your resolved gh login, a UTC timestamp
    stamped by this command (never supplied in the payload — `recorded_at` is
    rejected as an unknown key), the finding's verbatim summary, and the
    reviewed sha it was raised against. An entry with no resolvable sha is
    refused; pass --reviewed-sha for a hand-written payload.

    The marker is ADDITIVE across comments: the reader unions every marker on
    the thread, so post only what you are settling now rather than re-posting
    the whole ledger. Only `REJECTED` suppresses a later re-raise; `ACCEPTED`
    is a record-only annotation that reaches the reviewer's prompt.

    Emits one audit event per settled finding — `review.finding_settled`, or
    `review.finding_disposition_reverted` for a `REVERSED` entry (#2232) —
    correlated to --ticket when given. The events are recorded BEFORE the
    marker is written: if any of them cannot be recorded the settle is
    refused outright — no marker, no --out file, non-zero exit — because a
    durable suppression with no audit record is invisible.

    On success: exits 0, prints the marker to stdout.
    On failure: exits 1, prints 'field.path: message' lines to stderr.
    """
    _refuse_settle_outside_an_interactive_session()
    settle_reason = reason.strip()
    if not settle_reason:
        msg = (
            "--reason must be non-blank: a suppression with no recorded "
            "rationale is exactly the silent silencing this record exists to "
            "prevent."
        )
        raise CwError(msg)
    actor = _resolve_settle_actor()
    recorded_at = _utc_now_iso()

    parsed = _parse_payload_or_exit(path, _SettleInput)
    ledger = build_finding_disposition_ledger(
        (
            entry.file,
            entry.summary,
            FindingDisposition(
                outcome=entry.outcome,
                rationale=entry.rationale.strip() or settle_reason,
                recorded_at=recorded_at,
                actor=actor,
                reviewed_sha=_settle_reviewed_sha(entry, reviewed_sha.strip()),
                summary=entry.summary,
            ),
        )
        for entry in parsed.entries
    )
    # The audit events go FIRST, and the marker is written only once every one
    # of them has recorded. The two failure directions are not symmetric: an
    # audit record with no effect is noise, a durable suppression with no audit
    # record is invisible — which is the exact hole round 1 added this event to
    # close. Do not "fix" this back to save-then-emit: #1617's precedent orders
    # a STATE MUTATION before its event so the event cannot claim something
    # that did not land, and here the record IS the safety mechanism, so it
    # goes first. Atomicity is all-events-then-one-write for the same reason:
    # if any event fails, no marker is written at all.
    _emit_settle_events(ledger, ticket)
    rendered = render_finding_disposition_block(ledger)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(out, rendered)
    click.echo(rendered)


class _VerifyFixesInput(BaseModel):
    """Request envelope for ``cw review verify-fixes`` (#1805).

    ``reviewed_sha`` (#1988) is the fix-cycle branch tip the payload's
    ``diff`` was captured against — only consumed when ``--base`` is passed,
    as the second argument to the ``git diff <base>...<reviewed_sha>``
    verification. It is independent of ``verdict.reviewed_sha`` (the
    Checkpoint-3a sha the verdict was frozen at); the command never
    cross-checks the two.
    """

    verdict: ReviewVerdict
    diff: str
    reviewed_sha: str


@review.command(name="verify-fixes")
@click.argument("path")
@click.option(
    "--worktree",
    default=None,
    type=click.Path(path_type=Path),
    help=(
        "Worktree root used to run the --base git-diff verification "
        "(defaults to the current directory)."
    ),
)
@click.option(
    "--base",
    default=None,
    type=str,
    help=(
        "Compare the payload's diff text against the real `git diff "
        "<base>...<reviewed_sha>` output and reject the payload if "
        "they differ, guarding against a hand-typed or corrupted "
        "diff. `base` is the sha the reviewed verdict was frozen at "
        "(e.g. the Checkpoint-3a tip); `reviewed_sha` is the "
        "payload's own fix-cycle branch tip. Resolves the repo root "
        "from --worktree, falling back to the current directory. "
        "Mutually exclusive with --no-base-check; exactly one of "
        "the two must be given."
    ),
)
@click.option(
    "--no-base-check",
    is_flag=True,
    default=False,
    help=_NO_BASE_CHECK_HELP,
)
@handle_errors
def review_verify_fixes(
    path: str,
    worktree: Path | None,
    base: str | None,
    no_base_check: bool,
) -> None:
    """Downgrade 'fixed' dispositions the fix-cycle diff does not substantiate.

    PATH is a file path or '-' for stdin. Payload: {"verdict": <the adjudicated
    ReviewVerdict>, "diff": "<raw unified diff text of the fix cycles>",
    "reviewed_sha": "<fix-cycle branch tip>"}.

    A "fixed" finding whose cited file/line the diff never touched becomes
    "dropped", with the reason in `disposition_detail`. Record-only: no gate
    is re-evaluated and no fix cycle is triggered — the caller surfaces the
    downgrade in friction_highlights.

    --base verifies the payload's diff text is byte-identical to the real
    `git diff <base>...<reviewed_sha>` output, resolved from --worktree (or
    the current directory), and rejects the payload otherwise. Exactly one
    of --base/--no-base-check must be given; --no-base-check skips this
    check entirely and is for tests and human recovery debugging only,
    never for pipeline use.

    On success: exits 0, prints the downgraded ReviewVerdict as JSON to stdout.
    On failure: exits 1, prints 'field.path: message' lines to stderr; exits
    2 if neither or both of --base/--no-base-check are given.
    """
    _require_base_xor_no_base_check(base, no_base_check)

    parsed = _parse_payload_or_exit(path, _VerifyFixesInput)
    _run_base_check_if_requested(parsed.diff, base, parsed.reviewed_sha, worktree)
    verdict = verify_fixed_dispositions(
        parsed.verdict, _build_captured_diff(parsed.diff)
    )
    click.echo(verdict.model_dump_json(indent=2))
