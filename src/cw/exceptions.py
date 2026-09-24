"""Exception hierarchy for cw."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

# ``datetime`` is deliberately imported at RUNTIME above rather than listed here
# (#1409 review round 1): ``parse_usage_limit_reset`` and
# ``UsageLimitError.__init__`` both annotate it, and ``from __future__ import
# annotations`` turns those annotations into strings — so a TYPE_CHECKING-only
# import makes ``typing.get_type_hints()`` on either one raise NameError.
# Sibling modules (``native_daemon``, ``dispatch/loop``) import it
# unconditionally for the same reason.
if TYPE_CHECKING:
    from pathlib import Path

    from cw.sprint import AppliedBuildout

# Usage-limit detection regex. Matches all documented Claude usage-limit phrasings:
# - "You've hit your session limit · resets 3:45pm"   (verified against errors.md)
# - "You've hit your weekly limit · resets Mon 12:00am" (verified against errors.md)
# - "You've hit your Opus limit · resets 3:45pm"      (verified against errors.md)
# Uses \S+ rather than an explicit allow-list so undocumented future limit types
# (e.g. "5-hour limit") are also detected. Replaces the narrower
# r"hit (?:your )?(?:session|usage) limit" from reconcile.py:126 which missed
# weekly and Opus variants.
#
# DELIBERATELY UNCHANGED by #1409: four consumers (native_daemon, queue_peek,
# reconcile/_shared, tests) depend on this broad "detect anything" match, so the
# reset-time fragment is parsed by the separate anchored companion below rather
# than by adding capture groups here.
USAGE_LIMIT_RE = re.compile(r"hit (?:your )?\S+ limit", re.IGNORECASE)

# Anchored companion to USAGE_LIMIT_RE: the "· resets <time>" fragment that
# follows a matched usage-limit phrase (#1409). Applied with .match() to a short
# window immediately after the detector's match, never searched over whole text.
#
# Hardening notes:
# - Only bounded quantifiers, and none nested, so there is no catastrophic
#   backtracking on adversarial input.
# - Explicit [0-9] rather than \d: int() accepts non-ASCII digits (e.g.
#   Arabic-Indic "٣"), and a reset time written in them is not something we want
#   to guess at.
# - Unicode \s is kept so the NNBSP/NBSP separators Claude renders still match.
# - The optional date prefix is a weekday (``Mon``) or a month-day (``Sep 26,``);
#   the month-day form is the wording a running session's mid-turn limit stop
#   prints (#2324).
USAGE_LIMIT_RESET_RE = re.compile(
    r"\W{0,8}resets\s{1,4}"
    r"(?:(?P<day>mon|tue|wed|thu|fri|sat|sun)[a-z]{0,6}\s{1,4}"
    r"|(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]{0,6}\.?"
    r"\s{1,4}(?P<mday>[0-9]{1,2}),?\s{1,4})?"
    r"(?P<hour>[0-9]{1,2})(?::(?P<minute>[0-9]{2}))?\s?(?P<mer>[ap])\.?m\b",
    re.IGNORECASE,
)

_DAYS_PER_WEEK = 7
# Weekday abbreviations in datetime.weekday() order (Monday == 0).
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# Month abbreviations in calendar order (January == index 0).
_MONTHS = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
# Every accepted reset lies strictly under this far ahead. The time-only and
# weekday forms cannot reach it; a month-day form can name any date, so it is
# held to the same bound rather than trusted.
_MAX_RESET_HORIZON = timedelta(days=_DAYS_PER_WEEK)
_HOURS_PER_MERIDIEM = 12
# Valid 12-hour-clock components. Kept as ranges so the membership tests read as
# domain checks rather than PLR2004 magic-number comparisons.
_MERIDIEM_HOURS = range(1, 13)
_MINUTES = range(60)
# How far past the detector's match the reset fragment may start. Long enough for
# the separator Claude renders ("·", " -- ", a newline) plus the fragment itself;
# short enough that an unrelated later "resets" cannot be picked up.
_USAGE_LIMIT_RESET_SCAN_CHARS = 96
# Only the tail of the input is scanned. Keeps the whole parse linear in a bounded
# amount of work no matter how much output the subprocess produced, and preserves
# last-occurrence-wins (the currently-active limit is the last one printed).
_USAGE_LIMIT_PARSE_MAX_CHARS = 65_536


def parse_usage_limit_reset(text: str, *, now: datetime) -> datetime | None:
    """Return the instant a Claude usage limit resets, or None (#1409).

    **The spawn-time wording is UNVERIFIED against a real capture.** No
    captured ``claude --bg`` usage-limit message exists, and a usage limit
    cannot be induced on demand. The recognised forms and every test fixture
    are derived from *interactive* Claude transcript wording — ``resets
    3:45pm``, ``resets Mon 12:00am``, ``resets 11pm (America/New_York)``. If
    the real spawn-time text differs, this returns None and dispatch keeps
    today's flat ``usage_limit_backoff_seconds`` back-off.

    *now* MUST be timezone-aware; production passes the host-local clock via
    ``native_daemon._local_now()`` (``datetime.now(_host_timezone())``), a real
    :class:`~zoneinfo.ZoneInfo` rather than a frozen offset, so the DST
    transition table is consulted at resolution time (review round 2). A naive
    *now* yields None rather than raising — this function is total by
    construction, since it runs on untrusted subprocess output at a spawn
    failure.

    Resolution rules (from the #1344 comment history, as narrowed for the
    spawn path):

    - **R1** — a bare wall-clock time is read in *now*'s own timezone and any
      trailing annotation (``(America/New_York)``, ``ET``) is ignored. The
      offset is resolved at the CANDIDATE's date, not at *now*'s, so pass a
      *now* whose ``tzinfo`` carries the zone's DST rules (a
      :class:`~zoneinfo.ZoneInfo`, as ``native_daemon._local_now`` supplies) —
      a fixed-offset *now* silently freezes today's offset onto a reset that
      may sit on the other side of a transition.
    - **R2** — a time-only form that has already passed yields None; it never
      rolls forward to tomorrow.
    - **R3, deliberately inverted** — a weekday form naming *today* at a time
      that has already passed also yields None. The literal R3 reading
      (~zero back-off) would make every 30s tick re-claim and re-revert the
      task, charging ``unproductive_attempts`` until the attempt ceiling
      parks it minutes later.
    - **R5** — the LAST usage-limit phrase in *text* is the anchor.
    - **Month-day** (#2324) — ``resets Sep 26, 11pm``, the wording a running
      session's mid-turn stop prints, names that date in *now*'s year (the
      next year once it has passed, for a reset read across New Year). R1
      applies unchanged: the wall clock is read in *now*'s zone, not the
      annotation's. A date 7 days or more away yields None.

    The result is always strictly future, structurally under 7 days out, and
    returned as an aware UTC instant (the ``usage_limited_until`` sidecar
    compares against ``datetime.now(UTC)``, and a naive value would be
    silently dropped there).
    """
    if now.utcoffset() is None:
        return None
    tail = text[-_USAGE_LIMIT_PARSE_MAX_CHARS:]
    # R5: the last limit phrase is the currently-active one. finditer over the
    # capped tail is linear, and the window handed to the anchored companion
    # regex below is a fixed 96 characters.
    anchors = list(USAGE_LIMIT_RE.finditer(tail))
    if not anchors:
        return None
    end = anchors[-1].end()
    fragment = USAGE_LIMIT_RESET_RE.match(
        tail[end : end + _USAGE_LIMIT_RESET_SCAN_CHARS]
    )
    if fragment is None:
        return None
    return _resolve_reset_candidate(fragment, now=now)


def _resolve_reset_candidate(
    fragment: re.Match[str], *, now: datetime
) -> datetime | None:
    """Turn a matched ``resets <time>`` fragment into an aware UTC instant.

    Split out of :func:`parse_usage_limit_reset` to keep both functions inside
    the PLR0911 return budget. Returns None for any component out of range, for
    any candidate that is not strictly in the future, and for any candidate
    :data:`_MAX_RESET_HORIZON` or further out.
    """
    hour = int(fragment["hour"])
    minute = int(fragment["minute"]) if fragment["minute"] else 0
    if hour not in _MERIDIEM_HOURS or minute not in _MINUTES:
        return None
    hour24 = hour % _HOURS_PER_MERIDIEM
    if fragment["mer"].lower() == "p":
        hour24 += _HOURS_PER_MERIDIEM

    candidate = _reset_candidate_on_named_date(
        fragment, now.replace(hour=hour24, minute=minute, second=0, microsecond=0)
    )
    if candidate is None:
        return None
    # fold is applied AFTER the date shift because timedelta arithmetic
    # resets it to 0. fold=1 selects the SECOND pass through an ambiguous wall
    # clock (the repeated hour on a fall-back day) -- the later instant, so the
    # spawn gate never reopens before the limit actually lifts. It has no
    # effect on an unambiguous time. On the other transition a named wall clock
    # inside the spring-forward gap does not exist at all; fold=1 then resolves
    # it with the post-transition offset, i.e. the EARLIER of the two readings,
    # which can only shorten the window (safe, and self-correcting on re-hit).
    candidate = candidate.replace(fold=1)
    # One check covers the time-only already-passed case (R2), the
    # weekday-names-today already-passed case (inverted R3), and a month-day
    # already passed this year; a positive weekday delta is always in the
    # future, so nothing else can reach here past.
    if candidate <= now or candidate - now >= _MAX_RESET_HORIZON:
        return None
    return candidate.astimezone(UTC)


def _reset_candidate_on_named_date(
    fragment: re.Match[str], today_at: datetime
) -> datetime | None:
    """Move *today_at* (the reset's wall clock, today) onto the named date.

    A weekday names the next such day (today included). A month-day names that
    date in the current year, or the next year when it has already passed --
    so ``resets Jan 2`` read on Dec 30 lands in January; the horizon check in
    :func:`_resolve_reset_candidate` rejects any other far-off roll. Returns
    None for an impossible month-day such as ``Feb 30``. No named date leaves
    *today_at* unchanged.
    """
    day = fragment["day"]
    if day is not None:
        delta = (_WEEKDAYS.index(day.lower()) - today_at.weekday()) % _DAYS_PER_WEEK
        return today_at + timedelta(days=delta)
    month = fragment["month"]
    if month is None:
        return today_at
    try:
        candidate = today_at.replace(
            # The regex accepts documented long month spellings (for example
            # ``Sept``) as well as abbreviations; the resolver uses the
            # canonical three-letter keys.
            month=_MONTHS.index(month.lower()[:3]) + 1,
            day=int(fragment["mday"]),
        )
        if candidate.date() < today_at.date():
            candidate = candidate.replace(year=candidate.year + 1)
    except ValueError:
        return None
    return candidate


class CwError(Exception):
    """Base exception for all cw errors."""

    __slots__ = ()


class WorktreeError(CwError):
    """Error from git worktree operations."""

    __slots__ = ()


class MissingWorkspaceError(WorktreeError):
    """Raised when a client's workspace directory does not exist.

    This is a config-hygiene condition (stale/misconfigured entry), not a git
    operation failure. Callers should treat it as a soft skip rather than an
    error that contributes to exit-1 semantics.
    """

    __slots__ = ()


class StaleWorktreeError(WorktreeError):
    """A pre-existing worktree is checked out on a branch other than requested.

    Raised only by ``create_worktree``'s idempotent-reuse guard (#404). Kept
    distinct from the base :class:`WorktreeError` so the dispatch loop can
    force-remove the stale tree and let the task retry — without conflating it
    with other git failures (notably the main-checkout guard) that must never
    trigger a removal.
    """

    __slots__ = ()


class BranchHeldByWorktreeError(WorktreeError):
    """``create_worktree`` found the requested branch already checked out in
    another worktree (git's "already used by worktree at '<path>'" fatal).

    Most often an orphaned harness ``Agent(isolation="worktree")`` workspace
    left behind at a path (e.g. under ``.claude/worktrees/agent-<id>``) that
    cw's own GC never scans, because it carries no ``Session``/``TicketTask``
    row for cw to recognize as live (#2017). This is a refusal + diagnostic
    report, not a removal (#2034) — the holder is never touched, since cw
    cannot tell whether it is still in use.
    """

    __slots__ = ("holder_path",)

    def __init__(self, message: str, *, holder_path: Path) -> None:
        super().__init__(message)
        self.holder_path = holder_path


class WorktreeOccupiedError(WorktreeError):
    """``create_worktree`` refused to hand back a worktree another worker may be using.

    Raised only by ``create_worktree``'s reuse refresh (``refresh_on_reuse=True``,
    #2213) when a live cw session in persisted state, a live daemon-roster
    worker, or an INDETERMINATE read of either (fail closed) is homed on the
    worktree. It carries the two facts a handler needs: ``path`` (the worktree)
    and ``reason`` (which occupant, or that occupancy could not be ruled out).

    It is an exception rather than a return value so a caller cannot get it
    wrong by forgetting to look: an ignored boolean or report field lets a spawn
    path carry on into an occupied tree (the dispatch claim path did exactly
    that). Every caller that would spawn into, dispatch against, or mutate the
    worktree must abort.

    It is deliberately NOT a :class:`StaleWorktreeError`. The dispatch claim path
    force-removes a stale worktree on that branch; an occupied one must never be
    removed, only left for a later attempt. A tree that is merely dirty, diverged
    or behind is NOT this error: that is "not refreshed", and the tree is the
    caller's to use as it is.
    """

    __slots__ = ("path", "reason")

    def __init__(self, message: str, *, path: Path, reason: str) -> None:
        super().__init__(message)
        self.path = path
        self.reason = reason


class HookContextConflictError(CwError):
    """Hook-context injection refused to overwrite a worktree's existing state.

    Two functionally distinct raise reasons, both from
    :func:`cw.spawn._write_hook_context`:

    1. **USER-origin settings conflict** — a user-owned
       ``.claude/settings.local.json`` already exists in the worktree. The
       user-managed file must not be silently clobbered with the cw Stop-hook
       template; callers (Phase C of multiplexer-removal) route this to a
       clean failure mode rather than overwriting.
    2. **DAEMON-origin live-session conflict** — the worktree's existing
       ``.claude/cw-context.json`` references a session that is still
       non-terminal in cw state, so its hook context must not be stolen
       (issue #427 fix 2).

    Only reason 2 supplies ``conflicting_session_id`` — the id of the session
    that blocks the reuse. The dispatch claim path stamps it onto the owning
    task so concierge recipe 1 can refuse to requeue a row it already proved
    cannot spawn until that session is closed (GitHub #1674). It stays None
    for reason 1, whose raise site has no session to name.
    """

    __slots__ = ("conflicting_session_id",)

    def __init__(
        self, message: str, *, conflicting_session_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.conflicting_session_id = conflicting_session_id


class RemoteRefUnresolvedError(CwError):
    """No remote ref could be verified for a fix-loop branch (GitHub #2209).

    Raised by :func:`cw.reconcile.review_recipes.fix_agent.dispatch_fix_agent`
    when no candidate in its reported/upstream/templated ladder has a tip equal
    to the worktree's HEAD — either because nothing resolves at all, or because
    every ref that does resolve is stale.

    A typed subclass so ``cw.reconcile.fix_dispatch`` can discriminate this one
    class without matching message text: every other ``CwError`` keeps the
    generic clear-the-handoff-and-revert path, while this one parks the row
    BLOCKED_ON_USER with the action list retained. The split matters because
    since #2075 the generic path charges no attempt, so a ref that can never
    resolve would otherwise loop review -> failed dispatch -> review forever.
    """

    __slots__ = ()


class DisclaimerNotAcceptedError(CwError):
    """Raised when ``claude --bg`` fails because the user has not accepted
    the bypass-permissions disclaimer.

    Detection: the substring ``"requires accepting the disclaimer first"`` is
    present in ``CalledProcessError.stderr``. Verified against the live
    ``claude`` binary at version 2.1.150 via ``strings`` — the full stderr line
    emitted by that binary is::

        --bg with bypassPermissions requires accepting the disclaimer first.
        Run `claude --dangerously-skip-permissions` once interactively.

    Remediation: run ``claude --dangerously-skip-permissions`` once interactively
    to accept the disclaimer (persisted to ``~/.claude/settings.json`` as
    ``skipDangerousModePermissionPrompt: true``).
    """

    __slots__ = ()


class LaneMoveError(CwError):
    """Raised when a ticket cannot be moved due to its current status."""

    __slots__ = ()


class LaneNotFoundError(CwError):
    """Raised when a target lane is not declared for the client."""

    __slots__ = ()


class ConfigValidationError(CwError):
    """A config-facing Pydantic model (``ClientConfig``, ``OrchestratorConfig``,
    etc.) failed validation while loading ``clients.yaml`` or
    ``orchestrator.yaml`` (GitHub #1200).

    Raised by :func:`cw.config.load_clients` and
    :func:`cw.config.load_orchestrator_config`, wrapping the underlying
    ``pydantic.ValidationError`` so callers (the CLI boundary's
    ``handle_errors``, the dispatch loop's guarded config reload, ``cw
    doctor``'s loader-failure checks) can catch one ``CwError`` subclass
    instead of reaching across the pydantic import boundary. The message
    names the offending file and, via the wrapped pydantic error text, the
    specific field/key that failed -- e.g. an ``extra="forbid"`` rejection of
    a typo'd config key.
    """

    __slots__ = ()


class UsageLimitError(CwError):
    """Raised when ``claude --bg`` fails because a fleet-wide usage limit is active.

    Detection: output matches :data:`USAGE_LIMIT_RE`. Both spawn-time
    (``CalledProcessError`` path) and post-spawn (stdout without session ID) paths
    raise this error.

    Back-off: callers (dispatch loop) set a ``usage_limited_until`` window and skip
    further spawns until it elapses. See :func:`cw.dispatch.run_dispatch_loop`.

    ``reset_at`` carries the instant the limit lifts, when the spawn-time output
    named one that :func:`parse_usage_limit_reset` could resolve (#1409). It is
    parsed once at the raise site in ``cw.native_daemon``, from the RAW
    subprocess text rather than from ``message`` (the stdout branch embeds a
    ``repr()``). It stays None whenever the text carried no resolvable reset,
    which is the signal for the dispatch loop to fall back to the flat
    ``usage_limit_backoff_seconds`` window. Keyword-only and defaulted, so every
    pre-existing message-only raiser stays valid.
    """

    __slots__ = ("reset_at",)

    def __init__(self, message: str, *, reset_at: datetime | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class SpawnUnregisteredError(CwError):
    """Raised when a spawned worker never appears in the daemon roster.

    After ``claude --bg`` returns a short session id, cw polls the daemon
    roster to verify the supervisor actually adopted the worker. When the id
    is absent after the polling window elapses, this error is raised instead
    of leaving a phantom RUNNING session that burns a 30-minute idle cycle
    before the watchdog reaps it.

    The caller (dispatch loop) handles it the same as any broad spawn
    failure: revert the task to PENDING for retry. A distinct
    ``SESSION_SPAWN_UNREGISTERED`` event is emitted before the raise so the
    failure is diagnosable in the event inbox.
    """

    __slots__ = ()


class ApproveGateError(CwError):
    """Raised when a ticket cannot be approved because it is not at an approval gate."""

    __slots__ = ()


class DuplicatedHunkError(CwError):
    """The same hunk appears twice for the same file in a consolidate payload.

    Raised by ``cw.cli.review._diff_integrity._check_no_duplicate_hunks``
    (#1924). A hand-assembled envelope whose ``diff`` was reconstructed from
    memory can repeat a hunk verbatim; the matcher then validates evidence
    against a diff that no commit ever produced. Distinct files carrying
    byte-identical hunk text are legitimate and never raise — the file path is
    part of the duplicate key.
    """

    __slots__ = ()


class PlaceholderDiffError(CwError):
    """A consolidate payload's ``diff`` never carried a real diff.

    Raised by ``cw.cli.review._diff_integrity._check_not_placeholder_diff``
    (#1924) for an unresolved template token (``<diff here>``,
    ``<insert diff>``, ``...``) or for text too short to be a diff that also
    carries no ``diff --git`` header. Deliberately narrow, mirroring
    ``cw.auto_dev_result.parse._is_placeholder_sentinel_text``: silently
    accepting a real diff matters more than catching every possible stub.
    """

    __slots__ = ()


class DiffBaseMismatchError(CwError):
    """A payload's ``diff`` is not the real diff for its base.

    Raised by ``cw.cli.review._diff_integrity._check_diff_matches_base``
    (#1924) when ``cw review consolidate --base <ref>`` or
    ``cw review verify-fixes --base <ref>`` (#1988) finds that the payload's
    diff text differs from
    ``git diff --no-color <base>...<reviewed_sha>``, or when that git
    invocation itself fails (an unresolvable ref).
    """

    __slots__ = ()


class DocumentsFromReadError(CwError):
    """A ``--documents-from`` source could not be read into documents.

    Raised by ``cw.cli.review.consolidate._resolve_documents_from_files`` and
    ``._load_reviewer_document`` (#1924) when the source path's parent does not
    exist, or when a matched file is unreadable, is not JSON, or does not
    validate as a ``ReviewerFindingsDocument``. The message always names the
    offending path so the operator can fix that one file rather than guess.
    """

    __slots__ = ()


class PlanReadError(CwError):
    """A ``cw review consolidate --plan`` source could not be read (#2101).

    Raised by ``cw.cli.review.consolidate._load_planned_files`` when the named
    plan file cannot be read from disk. Mirrors ``DocumentsFromReadError``'s
    shape for a different CLI option — the message always names the offending
    path.
    """

    __slots__ = ()


class EmitValidationError(CwError):
    """Raised by emit_result_locked() when the payload fails AutoDevResult
    validation. Carries the formatted field-error lines (see _format_errors
    in cw.result) so the cw result emit CLI wrapper can reproduce the
    existing 'field.path: message' stderr lines byte-identically without
    reaching back across the pydantic import boundary.
    """

    __slots__ = ("errors",)

    def __init__(self, message: str, *, errors: list[str]) -> None:
        super().__init__(message)
        self.errors = errors


class EmitSessionNotFoundError(CwError):
    """Raised by emit_result_locked() when the resolved session_id has no
    matching session in state. Carries the session id so callers (the CLI
    wrapper) can reconstruct the "Session '<id>' not found; no state was
    modified." message without re-deriving it.
    """

    __slots__ = ("session_id",)

    def __init__(self, message: str, *, session_id: str) -> None:
        super().__init__(message)
        self.session_id = session_id


class RequeueStateError(CwError):
    """Raised when a ticket cannot be requeued because it is not BLOCKED_ON_USER."""

    __slots__ = ()


class RequeueStageError(CwError):
    """Raised when requeue would regress a ticket to an earlier stage."""

    __slots__ = ()


class RequeueLiveSessionError(CwError):
    """Raised when a requeue is refused because a daemon-live session still
    exists for the ticket, or cannot be ruled out (GitHub #2275).

    Carries the live session ids (``Session.id``) as structured data, mirroring
    ``EmitSessionNotFoundError``'s shape, so a downstream catcher (auto_fix_ci's
    ``PR_ACTION_FAILED`` payload) does not have to re-parse the message.
    """

    __slots__ = ("session_ids",)

    def __init__(self, message: str, *, session_ids: tuple[str, ...]) -> None:
        super().__init__(message)
        self.session_ids = session_ids


class RequeueRosterUnreadableError(RequeueLiveSessionError):
    """Raised when a requeue is refused because the daemon roster at
    ``roster_path`` is unreadable or malformed, so a live session for the
    ticket cannot be ruled out (GitHub #2275, fail closed like #2213).

    A ``RequeueLiveSessionError`` subclass so every existing live-session
    catcher (CLI, ``drain``, ``auto_fix_ci``'s latch rollback) inherits the
    refusal; ``classify_requeue_live_session_error`` tags it distinctly.
    ``session_ids`` is always empty: no session is known to be live.
    """

    __slots__ = ("roster_path",)

    def __init__(self, message: str, *, roster_path: Path) -> None:
        super().__init__(message, session_ids=())
        self.roster_path = roster_path


class UnblockStateError(CwError):
    """Raised when a ticket cannot be unblocked because it is not park-marked."""

    __slots__ = ()


class DispatchServeError(CwError):
    """Raised when the dispatch supervisor exhausts its restart budget.

    Raised instead of ``sys.exit`` so the CLI boundary (``handle_errors``)
    owns the process-exit decision and programmatic callers get a catchable
    signal rather than a hard process kill.
    """

    __slots__ = ()


class VersionDriftError(DispatchServeError):
    """Raised when the dispatch loop detects it is running stale code.

    Caught by :func:`run_dispatch_serve` to trigger a clean restart without
    counting toward the crash cap — a version reload is intentional, not a
    crash.
    """

    __slots__ = ()


class DispatchLoopLockedError(CwError):
    """Raised when a dispatch loop is launched while another already holds the
    process-lifetime singleton lock (GitHub #1362).

    ``run_dispatch_loop`` acquires an advisory, non-blocking ``fcntl.flock``
    over ``DISPATCH_LOOP_LOCK`` at entry. A second launch (via ``cw dev-queue
    run`` or ``cw dev-queue serve``, including ``run --once``) fails fast with
    this error, whose message names the holding process's PID and normalized
    command so the operator can stop it or re-launch with ``--force``. A plain
    :class:`CwError` subclass — deliberately NOT a :class:`DispatchServeError`
    — so :func:`run_dispatch_serve` re-raises it immediately instead of
    swallowing it into its crash-restart/backoff loop.
    """

    __slots__ = ()


class RfcContractError(CwError):
    """An RFC does not satisfy the buildout input contract.

    Raised by :func:`cw.sprint.parse_rfc` when a required section or ticket
    field is absent, or when a ticket cites a decision/ticket/epic that the RFC
    does not define. The message always names the exact defect (e.g. "missing
    section: ## Tickets") so the operator can fix the RFC rather than guess.
    """

    __slots__ = ()


class SprintApplyError(CwError):
    """Raised when :func:`cw.sprint.apply_plan` cannot complete a `gh`
    issue-creation pass — any milestone/epic/ticket lookup or create call
    that reports failure (``ok=False`` or a ``None`` return).

    Carries the partial ``AppliedBuildout`` state accumulated before the
    failure via ``applied``, so the operator can see exactly what was already
    created or skipped and re-run ``cw sprint apply`` to resume rather than
    starting over (creation is idempotent by title). The type is only
    available under ``TYPE_CHECKING`` — ``cw.sprint`` imports from
    ``cw.exceptions``, so importing ``AppliedBuildout`` at runtime here would
    cycle; the annotation is deferred (``from __future__ import annotations``)
    so this never executes at import time.
    """

    __slots__ = ("applied",)

    def __init__(self, message: str, *, applied: AppliedBuildout | None = None) -> None:
        super().__init__(message)
        self.applied = applied


class SessionsLockReentryError(CwError):
    """Raised when ``sessions_lock()`` is re-entered on the same thread while
    already held.

    ``sessions_lock`` is a per-open-fd ``fcntl.flock``, which is not
    reentrant: a second acquisition on the same thread blocks forever in
    ``flock()`` against the fd already held by the outer acquisition
    (GitHub #1228 — the review-recipe act phase transitively re-entering
    ``reconcile()`` from inside its own locked body). Raising here, guarded
    by a thread-local flag checked before any second ``flock()`` syscall,
    converts that hang into a catchable error. Existing callers on the
    reentrant paths (``_dispatch_auto_fix_ci``, ``_dispatch_address_review``,
    ``_reconcile_usage_limited``) already catch ``CwError`` / broad
    ``Exception`` around the call that would otherwise re-enter, so no
    call-site changes are needed elsewhere.
    """

    __slots__ = ()


class ClaimTierArmingError(CwError):
    """Raised when the ledger's claim tier is armed with drift-checking off.

    The codex review ledger's claim-match suppression tier resolved enabled
    for a task whose ``disposition_drift_check_enabled`` resolved ``False``
    (GitHub #2232). Drift-checking is what keeps a stale settle from silently
    suppressing a re-raised finding; arming the fuzzy tier without it removes
    that protection with no visible symptom, which is the exact failure mode
    ADR-0016 named as the reason a rollback and a drift signal are
    preconditions for ever arming the tier at all.

    Raised by :func:`cw.codex_background._run_codex_review_and_complete`, at
    the same point the claim tier's own enabling is resolved — the one place
    both values are in hand for the same task and lane. It reaches the
    operator through that daemon thread's existing ``_log.exception`` path
    rather than a dedicated blocked reason, the same as every other
    misconfiguration-shaped exception its broad ``except Exception:`` already
    catches. Deliberately narrow: this is one cross-field refusal, not a
    general config-validation framework. ``ConfigValidationError`` would be
    the wrong type — that one wraps a ``pydantic.ValidationError`` at LOAD
    time, and both config files loaded cleanly here.
    """

    __slots__ = ()
