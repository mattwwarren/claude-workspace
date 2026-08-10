"""The ``run_doctor`` orchestrator that assembles the full doctor report.

Every individual check lives in a leaf cluster — ``config_checks``,
``linkage``, ``wedge``, ``loop_health``, ``skills_drift``, ``versions``. This
module holds only :func:`run_doctor`, which reaches each cluster through
direct submodule imports (never through the package ``__init__``) so the
package import graph stays acyclic. The one-directional discipline holds:
``cw.dispatch`` never imports ``cw.doctor``.
"""

from __future__ import annotations

import yaml
from pydantic import ValidationError

from cw import __version__
from cw.doctor import _deps
from cw.doctor._shared import DoctorReport
from cw.doctor.agent_spec_drift import _check_agent_spec_drift
from cw.doctor.config_checks import (
    _check_attention_state_census,
    _check_config_file,
    _check_dev_queue,
    _check_inbox_size,
    _check_orchestrator_config,
    _check_project_configs,
    _check_review_recipe_liveness,
    _check_review_strategy,
    _check_state_file,
)
from cw.doctor.linkage import (
    _check_cross_repo_rows,
    _check_dispatch_repo_head,
    _check_linkage,
    _check_reconcile,
    _check_workspace_paths,
    _check_worktree_paths_sessions,
)
from cw.doctor.loop_health import (
    _check_loop_health,
    _check_loop_liveness,
    _check_timed_out_merged,
)
from cw.doctor.skills_drift import _check_skills_commands_drift
from cw.doctor.versions import (
    _check_bypass_disclaimer,
    _check_claude_version,
    _check_codex_capability,
    _check_cw_deps,
    _check_cw_version,
    _check_daemon_reachable,
    _check_ssh_key_loaded,
)
from cw.doctor.wedge import (
    _check_wedge_active_no_daemon_entry,
    _check_wedge_dead_session_blocked_on_user,
    _check_wedge_repo_ahead,
    _check_wedge_task_running_completed_session,
    _check_wedge_task_running_no_session,
    _reap_wedge_findings,
)
from cw.exceptions import CwError


def run_doctor(*, reap: bool = False) -> DoctorReport:
    """Run every preflight check and return a populated report.

    When *reap* is True, also run state reconciliation and append a
    ``reconciliation`` check summarising the number of reaped sessions and
    reverted tickets. Also runs wedge checks and applies reap recipes.

    Linkage drift checks (parent/worker reference integrity) are always run,
    independent of the *reap* flag.
    """
    report = DoctorReport(version=__version__)
    report.checks.append(_check_config_file())
    report.checks.append(_check_orchestrator_config())
    # Per-client tracker config. A broken clients.yaml is already surfaced by
    # _check_config_file; degrade to no clients rather than crash the run.
    try:
        _clients = _deps.load_clients()
    except (OSError, yaml.YAMLError, CwError, ValidationError):
        _clients = {}
    report.checks.extend(_check_project_configs(_clients))
    report.checks.extend(_check_review_strategy(_clients))
    report.checks.extend(_check_agent_spec_drift(_clients))
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
    report.checks.append(_check_skills_commands_drift())
    report.checks.append(_check_codex_capability())
    report.checks.append(_check_daemon_reachable())
    report.checks.append(_check_ssh_key_loaded())
    report.checks.extend(_check_loop_health())
    report.checks.extend(_check_loop_liveness())
    report.checks.append(_check_inbox_size())
    report.checks.extend(_check_workspace_paths())
    report.checks.extend(_check_dispatch_repo_head(_clients))
    report.checks.extend(_check_cross_repo_rows(_clients))
    report.checks.extend(_check_worktree_paths_sessions(link_state))

    if link_state is not None:
        report.checks.extend(_check_timed_out_merged(link_state, _clients))
        # Wedge checks: load queue once, run all three checks.
        queue = _deps.load_dev_queue()
        report.wedge_findings.extend(
            _check_wedge_task_running_no_session(link_state, queue)
        )
        report.wedge_findings.extend(
            _check_wedge_task_running_completed_session(link_state, queue)
        )
        report.wedge_findings.extend(_check_wedge_repo_ahead(link_state, queue))
        report.wedge_findings.extend(
            _check_wedge_dead_session_blocked_on_user(link_state, queue)
        )
        report.wedge_findings.extend(_check_wedge_active_no_daemon_entry(link_state))
        if reap and report.wedge_findings:
            _reap_wedge_findings(report.wedge_findings)

    if reap:
        report.checks.append(_check_reconcile())
    return report
