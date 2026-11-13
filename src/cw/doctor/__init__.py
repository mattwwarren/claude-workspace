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
  ``DoctorReport``) consumed by every cluster, plus ``ReapScope``, the
  frozen ``(client, ticket)`` a scoped ``--reap --ticket`` carries (#2716).
- ``_deps`` — cross-cluster external callables (``load_clients``,
  ``load_dev_queue``) reached through the module object so a single
  monkeypatch at ``cw.doctor._deps.NAME`` intercepts every caller.
- ``config_checks`` — config-file / project-config / review-recipe checks.
- ``linkage`` — session-state linkage, workspace, worktree, reconcile checks.
- ``wedge`` — wedge-condition detection and reap; a package with one
  submodule per detector family plus ``reap`` (#2164).
- ``routed_result_wedge`` — the stranded routed-result session class (#2524)
  and its explicit-command ``--reap`` close (the orchestrator session counts,
  ADR-0020).
- ``pending_skip_wedge`` — the advisory PENDING-row-held-by-an-occupied-
  worktree class (#2516); read-only, ``--reap`` changes nothing for it.
- ``lock_holds`` — the longest recent completed sessions / dev-queue lock
  holds and waits from the ``cw._lock_hold`` sidecar, with the aggregate
  state load/save share (#2637).
- ``session_outcomes`` — advisory no-result breakdown over the last 7 days;
  informational on the rate, WARN only when the inbox cannot be read (#2840).
- ``sessions_auto_prune`` — the last automatic sessions prune run from its
  ``sessions_auto_prune.json`` sidecar; advisory, WARN only on a failed run
  (#2965).
- ``worker_tmp_fallback`` — the shared ``~/.cw/tmp`` fallback mount's free GB
  and inodes, its tree size, and the last retention run; ok=False on a
  shortfall, WARN only on a failed retention run (#2682).
- ``loop_health`` — dispatch loop health/liveness, TIMED_OUT-merged detection,
  and targeted single-session reap.
- ``loop_code`` — the running dispatch loop's recorded code identity versus
  the checkout, an advisory warning when they differ (#2729).
- ``versions`` — claude/cw version + dependency checks, bypass disclaimer,
  daemon reachability.
- ``skills_drift`` — repo-tracked ``.claude/skills``/``.claude/commands`` vs
  ``~/.claude`` drift detection.
- ``agent_spec_drift`` — per-client reviewer agent-spec resolution (repo /
  global fallback / absent) drift detection.
- ``codex_fix_loop`` — per-lane effective codex fix-loop state and its source
  (lane vs global default) for lanes with a codex review executor (#2542).
- ``tracker_mcp`` — per-lane tracker-MCP reachability for opencode and codex
  lanes on a non-GitHub tracker: FAIL when the gate would park the row (#3055).
- ``all_codex_lane`` — per-lane advisory for lanes whose stages all run on
  codex: the clean-mode confirm default's ``claude`` dependency, the Claude-free
  opt-outs and the tracker MCP pointer (RFC 0018 D1, #3064).
- ``review_convergence`` — per-lane effective ``review.convergence: clean``
  support for clean clients: FAIL for a lane whose REVIEW backend writes no
  convergence ledger (#2797).
- ``merge_gate`` — clients with ``merge_gate_enabled: false`` (the Stage 4a
  merge gate switched off) listed as advisory warnings (#2851).
- ``commit_hook`` — per-client advisory warning when ``.pre-commit-config.yaml``
  declares commit-stage hooks but ``git commit`` would run none: hook absent,
  not executable, bypassed by ``core.hooksPath`` or recording a dead
  ``INSTALL_PYTHON``; also the bounded probe ``cw dev-queue status`` prints
  (#2973). Also ``hook-install-python/<client>``: an advisory warning when any
  shared hook (``pre-push`` included) records an ``INSTALL_PYTHON`` inside one
  of the client's dispatch-worktree bases (#3076) or a missing or
  non-executable ``INSTALL_PYTHON`` (a ``pre-commit`` hook already reported as
  stale by ``commit-hook`` is not repeated) (#3077).
- ``repo_bare`` — per-client advisory warning when ``core.bare=true`` is set on a
  checkout that has a working tree (#2973).
- ``spared_workers`` — the global advisory listing unrecorded daemon roster
  workers older than the leaked-worker grace that the sweep spares (#3257).
- ``user_level_hooks`` — detection of a ``cw signal-stop`` Stop hook installed
  in ``~/.claude/settings{,.local}.json``, where it applies to every Claude
  session instead of just cw's worktrees (#2226).
- ``worker_tmpdir`` — detection of live cw daemon workers whose effective
  TMPDIR is outside their own worktree (settings ``env``, then ``/proc``
  environ guarded by the roster ``procStart``) (#2672).
- ``core`` — the ``run_doctor`` orchestrator that assembles the full report.
- ``report`` — human-readable and JSON rendering.
"""

from __future__ import annotations

from cw.doctor._shared import CheckResult, DoctorReport, ReapScope, WedgeFinding
from cw.doctor.agent_spec_drift import _check_agent_spec_drift
from cw.doctor.all_codex_lane import _check_all_codex_lane
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
    _gh_on_path,
    _tracker_system,
)
from cw.doctor.core import run_doctor
from cw.doctor.linkage import (
    _check_cross_repo_rows,
    _check_dispatch_repo_head,
    _check_linkage,
    _check_workspace_paths,
    _check_worktree_paths_sessions,
)
from cw.doctor.lock_holds import _check_lock_holds
from cw.doctor.loop_code import _check_loop_code_identity
from cw.doctor.loop_health import (
    _check_loop_health,
    _check_loop_liveness,
    _check_timed_out_merged,
    _reap_session_by_selector,
)
from cw.doctor.merge_gate import _check_merge_gate_disabled
from cw.doctor.repo_bare import _check_repo_bare
from cw.doctor.report import format_report, format_report_json
from cw.doctor.review_convergence import _check_review_convergence
from cw.doctor.session_outcomes import _check_session_outcomes
from cw.doctor.sessions_auto_prune import _check_sessions_auto_prune
from cw.doctor.skills_drift import _check_skills_commands_drift
from cw.doctor.spared_workers import _check_spared_unrecorded_workers
from cw.doctor.tracker_mcp import _check_tracker_mcp
from cw.doctor.user_level_hooks import _check_user_level_stop_hook
from cw.doctor.versions import (
    _CW_DEPS_CHECK_NAME,
    _CW_DEPS_DRIFT_CHECK_NAME,
    _CW_PACKAGE_NAME,
    _CW_REINSTALL_CMD,
    _CW_VERSION_CHECK_NAME,
    _check_claude_version,
    _check_codex_capability,
    _check_cw_deps,
    _check_cw_deps_drift,
    _check_cw_version,
    _check_ssh_key_loaded,
    _dep_distribution_name,
)
from cw.doctor.wedge import (
    _check_wedge_repo_ahead,
    _check_wedge_task_running_completed_session,
    _check_wedge_task_running_no_session,
    _reap_wedge_findings,
)
from cw.doctor.worker_tmp_fallback import _check_worker_tmp_fallback
from cw.doctor.worker_tmpdir import _check_worker_tmpdir_scope

__all__ = [
    "_CW_DEPS_CHECK_NAME",
    "_CW_DEPS_DRIFT_CHECK_NAME",
    "_CW_PACKAGE_NAME",
    "_CW_REINSTALL_CMD",
    "_CW_VERSION_CHECK_NAME",
    "CheckResult",
    "DoctorReport",
    "ReapScope",
    "WedgeFinding",
    "_check_agent_spec_drift",
    "_check_all_codex_lane",
    "_check_attention_state_census",
    "_check_claude_version",
    "_check_codex_capability",
    "_check_codex_fix_loop",
    "_check_commit_hook",
    "_check_config_file",
    "_check_cross_repo_rows",
    "_check_cw_deps",
    "_check_cw_deps_drift",
    "_check_cw_version",
    "_check_dev_queue",
    "_check_dispatch_repo_head",
    "_check_hook_install_python",
    "_check_inbox_size",
    "_check_linkage",
    "_check_lock_holds",
    "_check_loop_code_identity",
    "_check_loop_health",
    "_check_loop_liveness",
    "_check_merge_gate_disabled",
    "_check_orchestrator_config",
    "_check_project_configs",
    "_check_repo_bare",
    "_check_review_convergence",
    "_check_review_recipe_liveness",
    "_check_review_strategy",
    "_check_session_outcomes",
    "_check_sessions_auto_prune",
    "_check_sessions_size",
    "_check_skills_commands_drift",
    "_check_spared_unrecorded_workers",
    "_check_ssh_key_loaded",
    "_check_timed_out_merged",
    "_check_tracker_mcp",
    "_check_user_level_stop_hook",
    "_check_wedge_repo_ahead",
    "_check_wedge_task_running_completed_session",
    "_check_wedge_task_running_no_session",
    "_check_worker_tmp_fallback",
    "_check_worker_tmpdir_scope",
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
