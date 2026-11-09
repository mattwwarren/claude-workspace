"""The ``run_doctor`` orchestrator that assembles the full doctor report.

Every individual check lives in a leaf cluster — ``config_checks``,
``linkage``, ``wedge``, ``loop_health``, ``skills_drift``, ``versions``. This
module holds only :func:`run_doctor`, which reaches each cluster through
direct submodule imports (never through the package ``__init__``) so the
package import graph stays acyclic. The one-directional discipline holds:
``cw.dispatch`` never imports ``cw.doctor``.
"""

from __future__ import annotations

import dataclasses
import re
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from cw import __version__
from cw.doctor import _deps
from cw.doctor._shared import (
    REAP_COMMAND,
    CheckResult,
    DoctorReport,
    ReapScope,
    WedgeFinding,
)
from cw.doctor.agent_spec_drift import _check_agent_spec_drift
from cw.doctor.codex_fix_loop import _check_codex_fix_loop
from cw.doctor.commit_hook import _check_commit_hook, _check_hook_install_python
from cw.doctor.config_checks import (
    _check_attention_state_census,
    _check_config_file,
    _check_dev_queue,
    _check_inbox_size,
    _check_orchestrator_config,
    _check_project_configs,
    _check_review_recipe_liveness,
    _check_review_strategy,
    _check_sessions_size,
    _check_state_file,
    _check_worker_tmp_pressure,
)
from cw.doctor.linkage import (
    _check_cross_repo_rows,
    _check_dispatch_repo_head,
    _check_linkage,
    _check_reconcile,
    _check_workspace_paths,
    _check_worktree_paths_sessions,
)
from cw.doctor.lock_holds import _check_lock_holds
from cw.doctor.loop_code import _check_loop_code_identity
from cw.doctor.loop_health import (
    _check_loop_health,
    _check_loop_liveness,
    _check_timed_out_merged,
)
from cw.doctor.merge_gate import _check_merge_gate_disabled
from cw.doctor.pending_skip_wedge import _check_wedge_pending_worktree_occupied
from cw.doctor.repo_bare import _check_repo_bare
from cw.doctor.review_convergence import _check_review_convergence
from cw.doctor.review_engine import _check_review_engine
from cw.doctor.routed_result_wedge import (
    _check_wedge_routed_result_session,
    has_pending_routed_result_audits,
)
from cw.doctor.session_outcomes import _check_session_outcomes
from cw.doctor.sessions_auto_prune import _check_sessions_auto_prune
from cw.doctor.skills_drift import _check_skills_commands_drift
from cw.doctor.user_level_hooks import _check_user_level_stop_hook
from cw.doctor.versions import (
    _check_bypass_disclaimer,
    _check_claude_version,
    _check_codex_capability,
    _check_cw_deps,
    _check_cw_deps_drift,
    _check_cw_version,
    _check_daemon_reachable,
    _check_ssh_key_loaded,
)
from cw.doctor.wedge import (
    _check_wedge_active_daemon_stale_no_sentinel,
    _check_wedge_active_no_daemon_entry,
    _check_wedge_active_null_liveness_orphan,
    _check_wedge_dead_session_blocked_on_user,
    _check_wedge_leaked_daemon_worker,
    _check_wedge_operator_gated_park,
    _check_wedge_repo_ahead,
    _check_wedge_task_running_completed_session,
    _check_wedge_task_running_no_session,
    _check_wedge_terminal_sibling_park,
    _reap_wedge_findings,
)
from cw.doctor.worker_tmpdir import _check_worker_tmpdir_scope
from cw.exceptions import CwError
from cw.reconcile import ticket_id_for_session

if TYPE_CHECKING:
    from cw.models import ClientConfig, CwState, DevQueueStore

# The two session-reap marker names, written once: `_SCOPED_RAN_CHECKS` and
# `_SCOPED_SESSION_REAP_CHECKS` both derive from these (#2716).
_PHANTOM_SESSION_REAP_CHECK = "scoped-reap/phantom-session-reap"
_DAEMON_STALE_SESSION_REAP_CHECK = "scoped-reap/daemon-stale-session-reap"
# Marker checks a scoped `cw doctor --reap --ticket` report carries (#2716):
# each names one mutation the scoped reap ran (scoped to the named
# (client, ticket)) or skipped, with a `ran:` / `skipped:` detail.
_SCOPED_RAN_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "scoped-reap/running-task-revert",
        "ran: RUNNING rows reverted only for the named (client, ticket)",
    ),
    (
        "scoped-reap/blocked-on-user-collapse",
        "ran: BLOCKED_ON_USER rows collapsed only for the named (client, ticket)",
    ),
    (
        "scoped-reap/terminal-sibling-cancel",
        "ran: terminal_sibling parks cancelled only for the named (client, ticket)",
    ),
    (
        _PHANTOM_SESSION_REAP_CHECK,
        "ran: phantom sessions reaped only when their record is in scope",
    ),
    (
        _DAEMON_STALE_SESSION_REAP_CHECK,
        "ran: daemon-stale sessions reaped only when their record is in scope",
    ),
)
# The two markers whose work runs in the bounded session tail of the reap. A
# `sessions_lock` timeout there leaves them incomplete, so their `ran:` detail
# is rewritten (`_scoped_ran_checks`); the queue-side markers ran before the tail.
_SCOPED_SESSION_REAP_CHECKS = frozenset(
    {_PHANTOM_SESSION_REAP_CHECK, _DAEMON_STALE_SESSION_REAP_CHECK}
)
_SCOPED_RAN_PREFIX = "ran:"
_SCOPED_INCOMPLETE_PREFIX = "attempted (incomplete, see wedge-reap):"
_SCOPED_NO_MATCH_PREFIX = "skipped (no matching finding):"
# Dynamic marker (its detail carries the resolved scope and the match count),
# so it is not part of `_SCOPED_RAN_CHECKS`. Warn-level when nothing matched:
# a mistyped `-c` / `--ticket` otherwise reads as a clean no-op.
_SCOPED_SCOPE_CHECK_NAME = "scoped-reap/scope"
_SCOPED_SKIPPED_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "scoped-reap/leaked-worker-sweep",
        "skipped: roster-driven, cannot be attributed to one (client, ticket)",
    ),
    (
        "scoped-reap/routed-result-close",
        "skipped: global session close with a global outbox retry",
    ),
    (
        "scoped-reap/routed-result-audit-outbox",
        "skipped: global audit outbox is not scoped to one ticket",
    ),
    (
        "scoped-reap/global-reconcile",
        "skipped: global phantom reap, ticket revert and sweep",
    ),
    (
        "scoped-reap/global-preflight-checks",
        "skipped: read-only but global and slow, irrelevant to one ticket",
    ),
    (
        "scoped-reap/advisory-detectors",
        "skipped: advisory classes 4, 9, 13 do not reap one ticket",
    ),
)


def _collect_wedge_findings(
    link_state: CwState, queue: DevQueueStore
) -> list[WedgeFinding]:
    """Run every wedge detector against *link_state*/*queue*; return findings.

    Extracted from :func:`run_doctor` (#2480) to keep it under the PLR0915
    statement budget (CLAUDE.md) after the class-10 (leaked-daemon-worker)
    check was added — every detector here was previously inlined directly
    into ``run_doctor``.

    :func:`_collect_scoped_wedge_findings` is a hand-copied subset of this
    list for the scoped reap (#2716); a reaping detector added here is absent
    from the scoped reap until it is copied there too (fail-closed).
    """
    findings: list[WedgeFinding] = []
    findings.extend(_check_wedge_task_running_no_session(link_state, queue))
    findings.extend(_check_wedge_task_running_completed_session(link_state, queue))
    findings.extend(_check_wedge_repo_ahead(link_state, queue))
    findings.extend(_check_wedge_dead_session_blocked_on_user(link_state, queue))
    findings.extend(_check_wedge_terminal_sibling_park(queue))
    findings.extend(_check_wedge_operator_gated_park(link_state, queue))
    findings.extend(_check_wedge_active_no_daemon_entry(link_state))
    findings.extend(_check_wedge_active_daemon_stale_no_sentinel(link_state, queue))
    findings.extend(_check_wedge_active_null_liveness_orphan(link_state, queue))
    findings.extend(_check_wedge_leaked_daemon_worker(link_state))
    findings.extend(_check_wedge_routed_result_session(link_state, queue))
    findings.extend(_check_wedge_pending_worktree_occupied(queue))
    return findings


# The exact global command phrase, not a bare `--reap`; the lookahead keeps a
# longer token (`cw doctor --reap-x`) from matching. The detectors hard-code the
# phrase, so this relies on their literals staying byte-equal to `REAP_COMMAND`
# (guarded by `TestRecipeLiteralDriftGuard`).
_GLOBAL_REAP_PHRASE = re.compile(re.escape(REAP_COMMAND) + r"(?![\w-])")


def _scope_finding_recipe(finding: WedgeFinding, scope: ReapScope) -> WedgeFinding:
    """Return *finding* with its recipe naming the scoped reap command (#2716).

    The detectors write ``Run: cw doctor --reap ...``; a reader (often an
    agent) following that line from a scoped report would run the GLOBAL reap
    and mutate other clients' and tickets' rows. Only the exact command phrase
    is replaced, with :meth:`ReapScope.reap_command`; a recipe that does not
    name it, and every other field (``replace`` keeps a subclass such as the
    class-12 finding intact), is unchanged.
    """
    recipe = _GLOBAL_REAP_PHRASE.sub(lambda _m: scope.reap_command(), finding.recipe)
    return dataclasses.replace(finding, recipe=recipe)


def _collect_scoped_wedge_findings(
    link_state: CwState, queue: DevQueueStore, scope: ReapScope
) -> list[WedgeFinding]:
    """Run the reaping wedge detectors for one ``(client, ticket)`` (#2716).

    A hand-copied subset of :func:`_collect_wedge_findings` (keep the two in
    step): only the classes the reap mutates (2, 3, 5, 6, 7, 8) plus class 12,
    the reap's occupied-worktree guard. A detector added there later is
    simply absent here, which fails closed (nothing outside the scope is
    reaped). Every returned recipe names the scoped reap command, not the
    global one (:func:`_scope_finding_recipe`). Rows are filtered by
    ``scope.covers(task.client, task.ticket_id)`` and sessions by their
    record's client and the ticket in their name. Classes 2, 3 and 5 get the
    FULL *link_state*: they resolve a row's ``session_id`` through every
    session, and a scoped state would make a live session look dead. Classes 6
    and 8 get the scoped state. The copies are shallow and every detector is
    read-only.
    """
    scoped_queue = queue.model_copy(
        update={
            "tasks": [t for t in queue.tasks if scope.covers(t.client, t.ticket_id)]
        }
    )
    scoped_state = link_state.model_copy(
        update={
            "sessions": [
                s
                for s in link_state.sessions
                if scope.covers(s.client, ticket_id_for_session(s.name))
            ]
        }
    )
    findings: list[WedgeFinding] = []
    findings.extend(_check_wedge_task_running_no_session(link_state, scoped_queue))
    findings.extend(
        _check_wedge_task_running_completed_session(link_state, scoped_queue)
    )
    findings.extend(_check_wedge_dead_session_blocked_on_user(link_state, scoped_queue))
    findings.extend(_check_wedge_terminal_sibling_park(scoped_queue))
    findings.extend(_check_wedge_active_no_daemon_entry(scoped_state))
    findings.extend(
        _check_wedge_active_daemon_stale_no_sentinel(scoped_state, scoped_queue)
    )
    findings.extend(_check_wedge_pending_worktree_occupied(scoped_queue))
    return [_scope_finding_recipe(f, scope) for f in findings]


def _scoped_ran_checks(
    reap_check: CheckResult | None, *, reaped: bool
) -> list[tuple[str, str]]:
    """The ``ran:`` markers, truthful about what the reap did (#2716).

    The markers are static (they name what the scoped reap does). Two cases
    must not claim a completed mutation:

    - *reaped* is False (nothing matched the scope, so the reap was never
      invoked): every marker's leading ``ran:`` becomes ``skipped (no matching
      finding):``.
    - *reap_check* is a failing ``wedge-reap`` check (a bounded
      ``sessions_lock`` timeout in the session tail): the two session-reap
      markers' leading ``ran:`` becomes ``attempted (incomplete, see
      wedge-reap):``. The queue-side markers ran before the tail and stay
      ``ran:``.

    A reap that ran and succeeded returns the static markers unchanged.
    """
    if not reaped:
        return [
            (
                name,
                _SCOPED_NO_MATCH_PREFIX + detail.removeprefix(_SCOPED_RAN_PREFIX),
            )
            for name, detail in _SCOPED_RAN_CHECKS
        ]
    if reap_check is None or reap_check.ok:
        return list(_SCOPED_RAN_CHECKS)
    return [
        (
            name,
            _SCOPED_INCOMPLETE_PREFIX + detail.removeprefix(_SCOPED_RAN_PREFIX),
        )
        if name in _SCOPED_SESSION_REAP_CHECKS
        else (name, detail)
        for name, detail in _SCOPED_RAN_CHECKS
    ]


def _scope_check(scope: ReapScope, matched: int) -> CheckResult:
    """Echo the resolved scope and how many wedge findings it matched (#2716).

    ``ok`` stays True (a scope matching nothing is a valid no-op and the exit
    code is unchanged); ``warn`` flags the zero-match run so a mistyped
    ``--client`` or ``--ticket`` is visible in the report.
    """
    return CheckResult(
        _SCOPED_SCOPE_CHECK_NAME,
        ok=True,
        warn=matched == 0,
        detail=(
            f"ran: scope {scope.client}/{scope.ticket_id}; "
            f"{matched} wedge finding(s) matched"
        ),
    )


def _run_scoped_reap(scope: ReapScope, *, reap: bool) -> DoctorReport:
    """The scoped ``cw doctor --reap --ticket <T> --client <C>`` run (#2716).

    Runs only the state-file check, the scoped detectors and the scoped reap.
    The report echoes the resolved scope and the matched-finding count
    (``scoped-reap/scope``, warn-level on zero) right after the state-file
    check, then names every mutation it ran and skipped as ``scoped-reap/*``
    checks.
    The global preflight battery, the global ``reconcile()`` and the
    routed-result/leaked-worker paths are skipped. An unreadable
    ``sessions.json`` stops before any reap (the failing check is the
    report). The scoped findings are the report's ``wedge_findings``.
    """
    if not reap:
        msg = "a ReapScope only scopes a reap: call run_doctor(reap=True, scope=...)"
        raise ValueError(msg)
    report = DoctorReport(version=__version__)
    state_check, link_state = _check_state_file()
    report.checks.append(state_check)
    if link_state is None:
        return report
    queue = _deps.load_dev_queue()
    report.wedge_findings.extend(
        _collect_scoped_wedge_findings(link_state, queue, scope)
    )
    report.checks.append(_scope_check(scope, len(report.wedge_findings)))
    reap_check: CheckResult | None = None
    if report.wedge_findings:
        reap_check = _reap_wedge_findings(
            report.wedge_findings, reap_routed_result=False, scope=scope
        )
        if reap_check is not None:
            report.checks.append(reap_check)
    report.checks.extend(
        CheckResult(name, ok=True, detail=detail)
        for name, detail in (
            *_scoped_ran_checks(reap_check, reaped=bool(report.wedge_findings)),
            *_SCOPED_SKIPPED_CHECKS,
        )
    )
    return report


def _client_config_checks(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """Per-client config checks, in report order."""
    return [
        *_check_project_configs(clients),
        *_check_review_strategy(clients),
        *_check_agent_spec_drift(clients),
        *_check_codex_fix_loop(clients),
        *_check_review_convergence(clients),
        *_check_review_engine(clients),
        *_check_merge_gate_disabled(clients),
        *_check_commit_hook(clients),
        *_check_repo_bare(clients),
        *_check_hook_install_python(clients),
    ]


def run_doctor(
    *,
    reap: bool = False,
    routed_result_session_ids: set[str] | None = None,
    reap_routed_result: bool = True,
    scope: ReapScope | None = None,
) -> DoctorReport:
    """Run every preflight check and return a populated report.

    When *reap* is True, also run state reconciliation and append a
    ``reconciliation`` check summarising the number of reaped sessions and
    reverted tickets. Also runs wedge checks and applies reap recipes.

    Linkage drift checks (parent/worker reference integrity) are always run,
    independent of the *reap* flag.

    With *scope* (#2716) the run is the scoped reap of
    :func:`_run_scoped_reap` instead; *scope* requires ``reap=True``.
    """
    if scope is not None:
        return _run_scoped_reap(scope, reap=reap)
    report = DoctorReport(version=__version__)
    report.checks.append(_check_config_file())
    report.checks.append(_check_orchestrator_config())
    # Per-client tracker config. A broken clients.yaml is already surfaced by
    # _check_config_file; degrade to no clients rather than crash the run.
    try:
        _clients = _deps.load_clients()
    except (OSError, yaml.YAMLError, CwError, ValidationError):
        _clients = {}
    report.checks.extend(_client_config_checks(_clients))
    state_check, link_state = _check_state_file()
    report.checks.append(state_check)
    report.checks.append(_check_dev_queue())
    # #1201 anomaly layer: review-recipe liveness + attention-state census.
    report.checks.extend(_check_review_recipe_liveness(_clients))
    report.checks.append(_check_attention_state_census())

    # Linkage checks reuse the state already loaded by _check_state_file.
    # If state failed to load, state_check is ok=False and the user sees the
    # underlying problem; skipping linkage is correct (cascading from a
    # failed parse would just spam noise).
    if link_state is not None:
        report.checks.extend(_check_linkage(link_state))

    report.checks.append(_check_bypass_disclaimer())
    report.checks.append(_check_claude_version())
    report.checks.append(_check_cw_version())
    report.checks.append(_check_cw_deps())
    report.checks.append(_check_cw_deps_drift())
    report.checks.append(_check_skills_commands_drift())
    report.checks.append(_check_user_level_stop_hook())
    report.checks.append(_check_codex_capability())
    report.checks.append(_check_daemon_reachable())
    report.checks.append(_check_ssh_key_loaded())
    report.checks.extend(_check_loop_health())
    report.checks.extend(_check_loop_liveness())
    report.checks.append(_check_loop_code_identity())
    report.checks.append(_check_inbox_size())
    report.checks.extend(
        [_check_sessions_size(), _check_lock_holds(), _check_sessions_auto_prune()]
    )
    report.checks.append(_check_session_outcomes(link_state))
    report.checks.extend(_check_worker_tmp_pressure(_clients))
    report.checks.append(_check_worker_tmpdir_scope())
    report.checks.extend(_check_workspace_paths())
    report.checks.extend(_check_dispatch_repo_head(_clients))
    report.checks.extend(_check_cross_repo_rows(_clients))
    report.checks.extend(_check_worktree_paths_sessions(link_state))

    if link_state is not None:
        report.checks.extend(_check_timed_out_merged(link_state, _clients))
        # Wedge checks: load queue once, run every check off it.
        queue = _deps.load_dev_queue()
        report.wedge_findings.extend(_collect_wedge_findings(link_state, queue))
        if reap and (report.wedge_findings or has_pending_routed_result_audits()):
            reap_check = _reap_wedge_findings(
                report.wedge_findings,
                routed_result_session_ids=routed_result_session_ids,
                reap_routed_result=reap_routed_result,
            )
            if reap_check is not None:
                report.checks.append(reap_check)

    if reap:
        report.checks.append(_check_reconcile())
    return report
