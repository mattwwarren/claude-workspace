"""The ``cw review`` commands without a seam of their own (#2048).

``register``, ``adjudicate``, ``check-voided``, and ``verify-fixes`` — the four
commands left after ``consolidate`` and the diff-integrity guards were given
their own submodules.

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
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import click
from pydantic import BaseModel, Field

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


def _stamp_voided_at(entry: VoidedFinding) -> VoidedFinding:
    """Fill a blank ``voided_at`` with now, leaving a supplied one alone.

    The coordinating session supplies the judgment; the CLI supplies the
    clock. Re-stamping an entry that already carries a date would rewrite
    history on every idempotent re-post.
    """
    if entry.voided_at.strip():
        return entry
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return entry.model_copy(update={"voided_at": now})


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
