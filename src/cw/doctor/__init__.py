"""cw doctor preflight — report environment health in one place.

When the environment is missing required binaries or the state file is
corrupted, every cw command fails with a cryptic error. `cw doctor` is
the one place to find out *what* is wrong before starting a session.

Returns structured results so the CLI can format them and tests can
assert on specific checks.

This package was split out of a single ``doctor.py`` module (#1313, part 1 of
2); the public ``from cw.doctor import X`` surface is preserved here via
re-exports. Submodules:

- ``_shared`` — result dataclasses (``CheckResult``, ``WedgeFinding``,
  ``DoctorReport``) consumed by every cluster.
- ``_deps`` — cross-cluster external callables (``load_clients``,
  ``load_dev_queue``) reached through the module object so a single
  monkeypatch at ``cw.doctor._deps.NAME`` intercepts every caller.
- ``config_checks`` — config-file / project-config / review-recipe checks.
- ``linkage`` — session-state linkage, workspace, worktree, reconcile checks.
- ``core`` — deliberately-oversized interim shim: wedge detection + reap, loop
  health/liveness, versions, daemon reachability, and ``run_doctor``. Part 2
  will split this further.
- ``report`` — human-readable and JSON rendering.
"""

from __future__ import annotations

from cw.doctor._shared import CheckResult, DoctorReport, WedgeFinding
from cw.doctor.config_checks import (
    _check_attention_state_census,
    _check_config_file,
    _check_dev_queue,
    _check_inbox_size,
    _check_orchestrator_config,
    _check_project_configs,
    _check_review_recipe_liveness,
    _check_review_strategy,
    _gh_on_path,
    _tracker_system,
)
from cw.doctor.core import (
    _CW_DEPS_CHECK_NAME,
    _CW_PACKAGE_NAME,
    _CW_REINSTALL_CMD,
    _CW_VERSION_CHECK_NAME,
    _check_claude_version,
    _check_cw_deps,
    _check_cw_version,
    _check_loop_health,
    _check_loop_liveness,
    _check_timed_out_merged,
    _check_wedge_repo_ahead,
    _check_wedge_task_running_completed_session,
    _check_wedge_task_running_no_session,
    _dep_distribution_name,
    _reap_session_by_selector,
    _reap_wedge_findings,
    run_doctor,
)
from cw.doctor.linkage import (
    _check_cross_repo_rows,
    _check_dispatch_repo_head,
    _check_linkage,
    _check_workspace_paths,
    _check_worktree_paths_sessions,
)
from cw.doctor.report import format_report, format_report_json

__all__ = [
    "_CW_DEPS_CHECK_NAME",
    "_CW_PACKAGE_NAME",
    "_CW_REINSTALL_CMD",
    "_CW_VERSION_CHECK_NAME",
    "CheckResult",
    "DoctorReport",
    "WedgeFinding",
    "_check_attention_state_census",
    "_check_claude_version",
    "_check_config_file",
    "_check_cross_repo_rows",
    "_check_cw_deps",
    "_check_cw_version",
    "_check_dev_queue",
    "_check_dispatch_repo_head",
    "_check_inbox_size",
    "_check_linkage",
    "_check_loop_health",
    "_check_loop_liveness",
    "_check_orchestrator_config",
    "_check_project_configs",
    "_check_review_recipe_liveness",
    "_check_review_strategy",
    "_check_timed_out_merged",
    "_check_wedge_repo_ahead",
    "_check_wedge_task_running_completed_session",
    "_check_wedge_task_running_no_session",
    "_check_workspace_paths",
    "_check_worktree_paths_sessions",
    "_dep_distribution_name",
    "_gh_on_path",
    "_reap_session_by_selector",
    "_reap_wedge_findings",
    "_tracker_system",
    "format_report",
    "format_report_json",
    "run_doctor",
]
