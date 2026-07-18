"""cw doctor preflight — report environment health in one place.

When the environment is missing required binaries or the state file is
corrupted, every cw command fails with a cryptic error. `cw doctor` is
the one place to find out *what* is wrong before starting a session.

Returns structured results so the CLI can format them and tests can
assert on specific checks.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import logging
import shutil
import subprocess as _sp
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from cw import __version__
from cw.config import (
    clients_file,
    load_clients,
    load_orchestrator_config,
    load_state,
    orchestrator_config_file,
    save_state,
    sessions_lock,
    state_file,
)
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.dispatch import TICK_STALE_SECONDS
from cw.events import inbox_path, read_events, record_event
from cw.exceptions import CwError
from cw.executor import (
    CODEX_NOT_FOUND,
    CODEX_VERSION_UNKNOWN,
    codex_capability_diagnosis,
)
from cw.gh import TIMED_OUT_MERGED_LOOKBACK_DAYS, pr_is_merged_for_ticket
from cw.models import (
    CompletionReason,
    DispatchSkipReason,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import _ROSTER_PATH, get_native_daemon_client
from cw.orchestrate import TickSummary, latest_tick_summary_by_client
from cw.pr_hydrate import _is_candidate, _parse_pr_url, _repo_slug_mismatch
from cw.reconcile import (
    SPAWN_GRACE_SECONDS,
    compute_drift,
    feature_branch_key,
    reconcile,
    ticket_id_for_session,
)
from cw.reconcile.review_recipes import (
    RECIPE_ATTENTION_STATES,
    RECIPE_FIRED_AT_GETTERS,
    resolve_review_recipe_enabled,
)
from cw.review_strategy import HANDLE_KEY_BY_MODE, RECOGNIZED_MODES
from cw.tracker import PROJECT_CONFIG_RELPATH, load_project_config_dict
from cw.worktree import _git_dir, get_head_branch

if TYPE_CHECKING:
    from cw.models import ClientConfig, CwState, DevQueueStore, Session, TicketTask


@dataclass(frozen=True)
class CheckResult:
    """One preflight check and whether it passed."""

    name: str
    ok: bool
    detail: str
    warn: bool = False


@dataclass(frozen=True)
class WedgeFinding:
    """A detected wedge condition with an actionable recipe."""

    wedge_class: str
    session_id: str | None
    ticket_id: str | None
    recipe: str
    state_file: str


@dataclass
class DoctorReport:
    """Aggregated output from :func:`run_doctor`."""

    version: str
    checks: list[CheckResult] = field(default_factory=list)
    wedge_findings: list[WedgeFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def clean(self) -> bool:
        """True only when every check is both ok and not warned."""
        return all(c.ok and not c.warn for c in self.checks)


# Path to Claude Code user settings — read for the disclaimer-acceptance flag.
_CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Minimum supported Claude Code version for native-daemon dispatch.
_MIN_CLAUDE_VERSION = (2, 1, 139)

# Number of components (major.minor.patch) required in a version string.
_VERSION_PARTS = 3

# Check name for the installed-vs-source cw version drift detector.
_CW_VERSION_CHECK_NAME = "cw-version"

# Check name for the declared-vs-installed dependency drift detector.
_CW_DEPS_CHECK_NAME = "cw-deps"

# Reinstall command surfaced in warnings when the installed cw is stale.
_CW_REINSTALL_CMD = "uv tool install --reinstall claude-workspace"

# Package name used for importlib.metadata lookups.
_CW_PACKAGE_NAME = "claude-workspace"

# Separator characters that terminate a PEP 508 dependency name (version
# specifiers, environment markers, whitespace) — mirrors _parse_version's
# lightweight, no-`packaging`-dependency parsing style.
_DEP_NAME_SEPARATORS = "<>=!~; "

# Number of consecutive FRESHNESS_GATE ticks required to declare a loop stall.
_LOOP_STALL_CONSECUTIVE_TICKS = 3

# Session statuses that represent an expected terminal lifecycle end-state.
# A missing worktree on a terminal session is normal (cleaned after merge);
# only non-terminal sessions with missing worktrees indicate a potential fault.
_TERMINAL_SESSION_STATUSES: frozenset[SessionStatus] = frozenset(
    {SessionStatus.COMPLETED, SessionStatus.TIMED_OUT}
)


# Tracker systems cw recognizes in .claude/project-config.yaml. Anything else
# is a config error: the headless worker would silently fall back to its
# built-in default (Linear MCP) and stall on OAuth (see #675 / project-config).
_RECOGNIZED_TRACKERS: frozenset[str] = frozenset({"github-issues", "linear"})

# Wedge class for BLOCKED_ON_USER tasks whose sessions are dead (OOM/crash path).
_WEDGE_BLOCKED_DEAD_SESSION = "wedge/blocked-on-user-dead-session"

# Wedge class for ACTIVE/IDLE sessions with no matching daemon entry (crash/SSH
# failure path that leaves roster absent but session still "active" in cw state).
_WEDGE_ACTIVE_NO_DAEMON_ENTRY = "wedge/active-no-daemon-entry"

_log = logging.getLogger(__name__)


def _gh_on_path() -> bool:
    """True when the ``gh`` binary is resolvable on PATH (testable seam)."""
    return shutil.which("gh") is not None


def _tracker_system(raw: object) -> object:
    """Extract ``tracking.primary.system`` from parsed YAML, or None if absent."""
    if not isinstance(raw, dict):
        return None
    tracking = raw.get("tracking")
    if not isinstance(tracking, dict):
        return None
    primary = tracking.get("primary")
    if not isinstance(primary, dict):
        return None
    return primary.get("system")


def _tracker_prereq_result(name: str, system: object, path: Path) -> CheckResult:
    """Build the CheckResult for a recognized tracker's prerequisite probe."""
    if system == "github-issues":
        if _gh_on_path():
            return CheckResult(name, ok=True, detail=f"github-issues ({path})")
        return CheckResult(
            name,
            ok=True,
            warn=True,
            detail="github-issues tracker but `gh` is not on PATH",
        )
    # linear: cw cannot deterministically probe the Linear MCP from here, so
    # surface it informationally rather than fail.
    return CheckResult(
        name,
        ok=True,
        detail=f"linear tracker ({path}); requires Linear MCP reachable in worker",
    )


def _check_project_configs(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """Validate each client's ``.claude/project-config.yaml`` tracker config.

    Per client, resolves the repo root (``repo_path`` when worktree-based, else
    ``workspace_path``), reads ``.claude/project-config.yaml``, and checks that
    ``tracking.primary.system`` is a recognized tracker whose prerequisites are
    present. An absent file warns (github-issues is the documented default);
    an unrecognized system or a parse failure is a hard failure.
    """
    results: list[CheckResult] = []
    for client_name, client in clients.items():
        root = client.repo_path or client.workspace_path
        path = root / PROJECT_CONFIG_RELPATH
        name = f"project-config/{client_name}"
        if not path.exists():
            results.append(
                CheckResult(
                    name,
                    ok=True,
                    warn=True,
                    detail=(
                        f"no project-config.yaml at {path}; headless workers"
                        " fall back to the legacy Linear MCP default and can"
                        " stall on OAuth — pin tracking.primary.system"
                        " (github-issues or linear)"
                    ),
                )
            )
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            results.append(CheckResult(name, ok=False, detail=f"parse failed: {exc}"))
            continue
        system = _tracker_system(raw)
        if system not in _RECOGNIZED_TRACKERS:
            results.append(
                CheckResult(
                    name,
                    ok=False,
                    detail=(
                        f"tracking.primary.system={system!r} is not recognized"
                        f" (expected one of {sorted(_RECOGNIZED_TRACKERS)})"
                    ),
                )
            )
            continue
        results.append(_tracker_prereq_result(name, system, path))
    return results


def _review_strategy_block(root: Path) -> object:
    """Return the raw ``review_strategy`` value from project-config.yaml, or None.

    Returns None (a "nothing to warn about" signal) for an absent file,
    unparseable YAML, a non-dict root, or an absent key — a YAML parse failure
    is already surfaced by ``_check_project_configs``, so this check stays quiet
    rather than double-reporting. The file-read walk itself is shared with
    every other project-config.yaml consumer via
    ``cw.tracker.load_project_config_dict``.
    """
    raw = load_project_config_dict(root)
    if raw is None:
        return None
    return raw.get("review_strategy")


def _review_strategy_warning(name: str, block: object) -> CheckResult | None:
    """Return a WARN CheckResult for a misconfigured review_strategy, else None.

    Never a hard fail: the runtime silently degrades a bad value to ``ci`` (see
    ``cw.review_strategy.resolve_review_strategy``), so doctor's job is only to
    surface the typo. Clean configs (absent, ``ci``, or a mode with its handle)
    return None so no line is emitted.
    """
    if block is None:
        return None
    if not isinstance(block, dict):
        return CheckResult(
            name, ok=True, warn=True, detail="review_strategy is not a mapping"
        )
    mode = block.get("mode")
    if mode not in RECOGNIZED_MODES:
        return CheckResult(
            name,
            ok=True,
            warn=True,
            detail=(
                f"review_strategy.mode={mode!r} is not recognized"
                f" (expected one of {sorted(RECOGNIZED_MODES)})"
                " — runtime degrades to ci"
            ),
        )
    handle_key = HANDLE_KEY_BY_MODE.get(mode)
    if handle_key is not None and not block.get(handle_key):
        return CheckResult(
            name,
            ok=True,
            warn=True,
            detail=(
                f"review_strategy.mode={mode!r} but {handle_key!r} handle is"
                " missing — request_reviewer will emit PR_ACTION_FAILED"
            ),
        )
    return None


def _check_review_strategy(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """Warn on a misconfigured review_strategy per client (RFC 0010 P4, #1099).

    Advisory-only: emits a WARN (never a FAIL) when ``review_strategy.mode`` is
    unrecognized, non-mapping, or names a ``repo_owner``/``reviewer_team`` mode
    with a missing handle. A clean or absent config emits nothing.
    """
    results: list[CheckResult] = []
    for client_name, client in clients.items():
        root = client.repo_path or client.workspace_path
        block = _review_strategy_block(root)
        warning = _review_strategy_warning(f"review-strategy/{client_name}", block)
        if warning is not None:
            results.append(warning)
    return results


# Check name for the #1201 review-recipe liveness/census anomaly checks.
_LIVENESS_CHECK_NAME = "review-recipe-liveness"
_CENSUS_CHECK_NAME = "attention-state-census"


def _check_review_recipe_liveness(
    clients: dict[str, ClientConfig],
) -> list[CheckResult]:
    """Warn when an enabled review recipe has candidates but has never fired.

    #1201 anomaly layer. For every ``(recipe, attention_state)`` pair, groups the
    enabled candidate rows by ``(client, lane, recipe)``; a group where *zero*
    rows carry a non-None ``<recipe>_fired_at`` latch is a liveness anomaly — the
    recipe is enabled and has work at its trigger attention_state yet has not
    fired within the current episode. A group with even one fired row is healthy
    (partial firing proves the recipe can fire) and is NOT warned. The latch is
    an already-persisted proxy for "fired this episode" (cleared by
    ``_clear_ended_episodes`` when the episode ends), so no event replay or
    config window is needed. Degrades to a single no-warn result when the config
    or dev-queue fails to load (both are surfaced by their own checks).
    """
    try:
        config = load_orchestrator_config()
    except (OSError, yaml.YAMLError, CwError, ValidationError):
        config = OrchestratorConfig()
    if not config.review_recipes_enabled:
        return [
            CheckResult(
                _LIVENESS_CHECK_NAME,
                ok=True,
                detail="review recipes disabled (master switch off)",
            )
        ]
    try:
        tasks = load_dev_queue().tasks
    except (OSError, json.JSONDecodeError, ValidationError):
        return [
            CheckResult(
                _LIVENESS_CHECK_NAME,
                ok=True,
                detail="dev_queue unreadable (see dev_queue.json check)",
            )
        ]
    groups: dict[tuple[str, str, str], list[TicketTask]] = {}
    for task in tasks:
        if not _is_candidate(task) or task.pr_state is None:
            continue
        for recipe, attention_state in RECIPE_ATTENTION_STATES.items():
            if task.pr_state.attention_state != attention_state:
                continue
            if not resolve_review_recipe_enabled(task, clients, recipe):
                continue
            groups.setdefault((task.client, task.lane, recipe), []).append(task)
    results: list[CheckResult] = []
    for (client, lane, recipe), group in sorted(groups.items()):
        firings = sum(
            1 for t in group if RECIPE_FIRED_AT_GETTERS[recipe](t) is not None
        )
        if firings == 0:
            results.append(
                CheckResult(
                    f"{_LIVENESS_CHECK_NAME}/{client}/{lane}/{recipe}",
                    ok=True,
                    warn=True,
                    detail=(
                        f"{len(group)} candidate(s) at "
                        f"{RECIPE_ATTENTION_STATES[recipe]} but recipe {recipe!r} "
                        "has not fired this episode"
                    ),
                )
            )
    if not results:
        return [
            CheckResult(
                _LIVENESS_CHECK_NAME,
                ok=True,
                detail="all enabled review recipes with candidates have fired",
            )
        ]
    return results


def _check_attention_state_census() -> CheckResult:
    """Warn when a hydrated, non-draft candidate PR carries no attention_state.

    #1201 R4. A non-draft candidate row whose ``pr_state`` is hydrated but whose
    ``attention_state`` is None means the derivation ladder classified nothing
    where it should have — an observability gap that would leave the row
    invisible to every attention-state consumer. Draft PRs (None by design),
    un-hydrated rows (``pr_state is None``), and terminal PRs (excluded by
    ``_is_candidate``) are all out of scope. Degrades to a no-warn result when
    the dev-queue fails to load (surfaced by its own check).
    """
    try:
        tasks = load_dev_queue().tasks
    except (OSError, json.JSONDecodeError, ValidationError):
        return CheckResult(
            _CENSUS_CHECK_NAME,
            ok=True,
            detail="dev_queue unreadable (see dev_queue.json check)",
        )
    missing = [
        t
        for t in tasks
        if _is_candidate(t)
        and t.pr_state is not None
        and not t.pr_state.is_draft
        and t.pr_state.attention_state is None
    ]
    if missing:
        ids = ", ".join(sorted(t.ticket_id for t in missing))
        return CheckResult(
            _CENSUS_CHECK_NAME,
            ok=True,
            warn=True,
            detail=(
                f"{len(missing)} non-draft hydrated PR(s) with no "
                f"attention_state: {ids}"
            ),
        )
    return CheckResult(
        _CENSUS_CHECK_NAME,
        ok=True,
        detail="all non-draft hydrated candidate PRs have an attention_state",
    )


def _check_config_file() -> CheckResult:
    """Verify the clients.yaml exists or that no clients is acceptable."""
    path = clients_file()
    if not path.exists():
        return CheckResult(
            "clients.yaml",
            ok=True,
            detail=f"not yet created at {path} (run `cw init`)",
        )
    try:
        load_clients()
    except (OSError, yaml.YAMLError, CwError, ValidationError) as exc:
        return CheckResult("clients.yaml", ok=False, detail=f"parse failed: {exc}")
    return CheckResult("clients.yaml", ok=True, detail=str(path))


def _check_orchestrator_config() -> CheckResult:
    """Verify orchestrator.yaml parses, mirroring _check_config_file above."""
    path = orchestrator_config_file()
    if not path.exists():
        return CheckResult(
            "orchestrator.yaml",
            ok=True,
            detail=f"not yet created at {path} (will be generated on first use)",
        )
    try:
        load_orchestrator_config()
    except (OSError, yaml.YAMLError, CwError, ValidationError) as exc:
        return CheckResult("orchestrator.yaml", ok=False, detail=f"parse failed: {exc}")
    return CheckResult("orchestrator.yaml", ok=True, detail=str(path))


def _check_state_file() -> tuple[CheckResult, CwState | None]:
    """Verify sessions.json parses, returning the loaded state for downstream consumers.

    Returning the parsed state avoids a second ``load_state()`` call in
    ``run_doctor``: linkage checks reuse the same parsed object. On parse
    failure the second tuple element is ``None`` and downstream checks that
    need state should skip themselves; the failure is already visible via
    the returned ``CheckResult``.
    """
    path = state_file()
    try:
        state = load_state()
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        return (
            CheckResult("sessions.json", ok=False, detail=f"load failed: {exc}"),
            None,
        )
    return CheckResult("sessions.json", ok=True, detail=str(path)), state


def _check_dev_queue() -> CheckResult:
    try:
        load_dev_queue()
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        return CheckResult("dev_queue.json", ok=False, detail=f"load failed: {exc}")
    return CheckResult("dev_queue.json", ok=True, detail="parseable")


def _check_inbox_size() -> CheckResult:
    """Warn when events/inbox.jsonl exceeds its configured size/line thresholds.

    Read-only: never mutates or prunes the inbox. Absent inbox is healthy
    (nothing has been recorded yet). See ``cw event prune`` (GitHub #856).
    """
    inbox = inbox_path()
    if not inbox.exists():
        return CheckResult("inbox-size", ok=True, detail="no inbox file")

    try:
        config = load_orchestrator_config()
    except (OSError, yaml.YAMLError, CwError, ValidationError):
        # Degrade to defaults rather than raising: a bad orchestrator.yaml is
        # already reported by _check_orchestrator_config() above. Letting it
        # propagate here would crash run_doctor() before that ok=False
        # result is ever printed. See GitHub #1200.
        config = OrchestratorConfig()
    size_bytes = inbox.stat().st_size
    with inbox.open("r", encoding="utf-8") as f:
        line_count = sum(1 for _ in f)

    problems: list[str] = []
    if size_bytes > config.inbox_size_warn_bytes:
        problems.append(
            f"size {size_bytes}B exceeds inbox_size_warn_bytes"
            f" ({config.inbox_size_warn_bytes}B)"
        )
    if line_count > config.inbox_line_count_warn:
        problems.append(
            f"{line_count} lines exceeds inbox_line_count_warn"
            f" ({config.inbox_line_count_warn})"
        )
    if problems:
        detail = "; ".join(problems) + " — run `cw event prune`"
        return CheckResult("inbox-size", ok=False, detail=detail)

    return CheckResult(
        "inbox-size", ok=True, detail=f"{size_bytes}B, {line_count} lines"
    )


def _check_linkage(state: CwState) -> list[CheckResult]:
    """Detect parent/worker linkage drift in session state.

    Returns one :class:`CheckResult` per drift type:

    * ``linkage/dangling-worker`` — an orchestrator's ``worker_session_ids``
      references a session ID absent from state.
    * ``linkage/dangling-parent`` — a worker's ``parent_session_id`` points at
      a session absent from state.
    * ``linkage/asymmetric`` — one side knows about the link but the other
      side doesn't (forward-only or reverse-only reference).

    All three results are always returned; each is ``ok=True`` when no drift
    of that type is detected.
    """
    # Index built once: O(1) membership (via .keys()) and lookup throughout.
    session_by_id: dict[str, Session] = {s.id: s for s in state.sessions}

    # --- dangling-worker: orchestrator.worker_session_ids → missing session ---
    dangling_worker_msgs: list[str] = [
        f"orchestrator {sess.id!r} references missing worker {wid!r}"
        " — remove the stale ID from worker_session_ids"
        for sess in state.sessions
        for wid in sess.worker_session_ids
        if wid not in session_by_id
    ]

    if dangling_worker_msgs:
        dw_detail = "; ".join(dangling_worker_msgs)
        dw_result = CheckResult("linkage/dangling-worker", ok=False, detail=dw_detail)
    else:
        dw_result = CheckResult("linkage/dangling-worker", ok=True, detail="")

    # --- dangling-parent: worker.parent_session_id → missing session ---
    dangling_parent_msgs: list[str] = [
        f"worker {sess.id!r} references missing parent {sess.parent_session_id!r}"
        " — clear parent_session_id or restore the parent session"
        for sess in state.sessions
        if sess.parent_session_id is not None
        and sess.parent_session_id not in session_by_id
    ]

    if dangling_parent_msgs:
        dp_detail = "; ".join(dangling_parent_msgs)
        dp_result = CheckResult("linkage/dangling-parent", ok=False, detail=dp_detail)
    else:
        dp_result = CheckResult("linkage/dangling-parent", ok=True, detail="")

    # --- asymmetric: one side of the link is missing ---
    # Build a map: parent_id → {set of worker IDs that claim it as parent}
    claimed_by: dict[str, set[str]] = {}
    for sess in state.sessions:
        parent_id = sess.parent_session_id
        if parent_id is not None and parent_id in session_by_id:
            claimed_by.setdefault(parent_id, set()).add(sess.id)

    # Forward check: orchestrator lists a worker, but worker doesn't claim it back.
    # Workers already caught as dangling are skipped (wid not in session_by_id).
    fwd_msgs: list[str] = []
    for sess in state.sessions:
        for wid in sess.worker_session_ids:
            worker = session_by_id.get(wid)
            if worker is None or worker.parent_session_id == sess.id:
                continue
            fwd_msgs.append(
                f"orchestrator {sess.id!r} lists worker {wid!r},"
                f" but worker's parent_session_id is {worker.parent_session_id!r}"
                " — update parent_session_id on the worker"
            )

    # Reverse check: worker claims this session as parent, but session
    # doesn't list the worker in its worker_session_ids.
    rev_msgs: list[str] = [
        f"worker {wid!r} claims parent {sess.id!r},"
        f" but {sess.id!r}'s worker_session_ids does not include it"
        " — add the worker ID to worker_session_ids"
        for sess in state.sessions
        for wid in claimed_by.get(sess.id, set())
        if wid not in sess.worker_session_ids
    ]

    asym_msgs = fwd_msgs + rev_msgs

    if asym_msgs:
        asym_detail = "; ".join(asym_msgs)
        asym_result = CheckResult("linkage/asymmetric", ok=False, detail=asym_detail)
    else:
        asym_result = CheckResult("linkage/asymmetric", ok=True, detail="")

    return [dw_result, dp_result, asym_result]


def _check_reconcile() -> CheckResult:
    """Run reconciliation and describe the outcome as a check result."""
    try:
        reconcile_report = reconcile()
    except CwError as exc:
        return CheckResult(
            "reconciliation",
            ok=False,
            detail=f"reconcile failed: {exc}",
        )
    reaped = len(reconcile_report.phantom_session_ids)
    reverted = len(reconcile_report.reverted_ticket_ids)
    if reaped == 0 and reverted == 0:
        return CheckResult("reconciliation", ok=True, detail="no phantoms")
    return CheckResult(
        "reconciliation",
        ok=True,
        detail=(
            f"reaped {reaped} session(s), reverted {reverted} ticket(s); "
            f"ids: {reconcile_report.phantom_session_ids}"
        ),
    )


def _check_workspace_paths() -> list[CheckResult]:
    """Verify each client's effective git directory exists."""
    try:
        clients = load_clients()
    except Exception:  # noqa: BLE001
        return []  # _check_config_file() already surfaces parse errors
    results = []
    for name, client in clients.items():
        git_dir = _git_dir(client)
        if not git_dir.exists():
            results.append(
                CheckResult(
                    f"workspace/{name}",
                    ok=False,
                    detail=f"path does not exist: {git_dir}",
                )
            )
    return results


def _check_dispatch_repo_head(
    clients: dict[str, ClientConfig],
) -> list[CheckResult]:
    """Check each client's dispatch repo HEAD is on its default branch."""
    results: list[CheckResult] = []
    for name, client in clients.items():
        try:
            branch = get_head_branch(client)
        except OSError as exc:
            results.append(
                CheckResult(
                    f"dispatch-repo-head/{name}",
                    ok=True,
                    warn=True,
                    detail=f"could not read HEAD: {exc}",
                )
            )
            continue
        if branch is None:
            git_dir = _git_dir(client)
            default = client.default_branch
            results.append(
                CheckResult(
                    f"dispatch-repo-head/{name}",
                    ok=True,
                    warn=True,
                    detail=(
                        f"repo HEAD is detached, expected '{default}'"
                        f" — run: git -C {git_dir} checkout {default}"
                    ),
                )
            )
        elif branch != client.default_branch:
            git_dir = _git_dir(client)
            default = client.default_branch
            results.append(
                CheckResult(
                    f"dispatch-repo-head/{name}",
                    ok=True,
                    warn=True,
                    detail=(
                        f"repo HEAD is on '{branch}', expected '{default}'"
                        f" — run: git -C {git_dir} checkout {default}"
                    ),
                )
            )
    return results


def _check_cross_repo_rows(
    clients: dict[str, ClientConfig],
) -> list[CheckResult]:
    """Advisory warn-only check: dev-queue rows whose client resolves to a
    different github repo than the row's ``pr_url`` (GitHub #1198).

    Every result is ``ok=True, warn=True`` so it never flips
    ``DoctorReport.ok`` (mirrors ``_check_dispatch_repo_head``). A broken queue
    is already surfaced by ``_check_dev_queue``, so a load failure here degrades
    to ``[]`` rather than double-reporting.
    """
    try:
        store = load_dev_queue()
    except (OSError, json.JSONDecodeError, ValidationError):
        return []
    results: list[CheckResult] = []
    for task in store.tasks:
        if task.pr_url is None:
            continue
        client = clients.get(task.client)
        if client is None:
            continue
        parsed = _parse_pr_url(task.pr_url)
        if parsed is None:
            continue
        pr_repo = parsed[0]
        client_repo = _repo_slug_mismatch(pr_repo, client.workspace_path)
        if client_repo is None:
            continue
        results.append(
            CheckResult(
                f"cross-repo/{task.ticket_id}",
                ok=True,
                warn=True,
                detail=(
                    f"row {task.ticket_id} client {task.client!r} workspace repo"
                    f" {client_repo!r} != pr_url repo {pr_repo!r} — a"
                    " worker-dispatching recipe would run in the wrong workspace"
                ),
            )
        )
    return results


def _check_worktree_paths_sessions(
    state: CwState | None = None,
) -> list[CheckResult]:
    """Verify each non-terminal session's worktree_path exists. Read-only, warn-only.

    Terminal sessions (COMPLETED, TIMED_OUT) have their worktrees cleaned up
    as part of normal lifecycle — a missing path is expected, not a fault.
    Only non-terminal sessions with a missing worktree path emit a warn.
    """
    if state is None:
        return []
    wt_paths: list[tuple[str, Path, SessionStatus]] = [
        (s.id, s.worktree_path, s.status)
        for s in state.sessions
        if s.worktree_path is not None
    ]
    total_checked = len(wt_paths)
    results: list[CheckResult] = []
    for session_id, wt, status in wt_paths:
        if status in _TERMINAL_SESSION_STATUSES:
            continue
        if not wt.exists():
            results.append(
                CheckResult(
                    f"worktree/{session_id}",
                    ok=True,
                    warn=True,
                    detail=f"path does not exist: {wt}",
                )
            )
    missing_count = len(results)
    results.append(
        CheckResult(
            "worktree/summary",
            ok=True,
            warn=False,
            detail=(
                f"{total_checked} sessions checked, {missing_count} missing worktrees"
            ),
        )
    )
    return results


# ---------------------------------------------------------------------------
# Wedge detection helpers
# ---------------------------------------------------------------------------


def _check_wedge_task_running_no_session(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect RUNNING queue tasks with no associated live session.

    Skips tasks within SPAWN_GRACE_SECONDS of creation — newly spawned
    tasks have not yet registered their session_id.
    """
    findings: list[WedgeFinding] = []
    _live = {SessionStatus.ACTIVE, SessionStatus.IDLE}
    live_session_ids = {s.id for s in state.sessions if s.status in _live}
    now = datetime.now(UTC)
    for task in queue.tasks:
        if task.status != QueueItemStatus.RUNNING:
            continue
        if task.session_id is None:
            created = task.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if (now - created).total_seconds() < SPAWN_GRACE_SECONDS:
                continue
            findings.append(
                WedgeFinding(
                    wedge_class="wedge/task-running-no-session",
                    session_id=None,
                    ticket_id=task.ticket_id,
                    recipe=(
                        "Queue task RUNNING with no matching session. "
                        "Run: cw doctor --reap to revert task to PENDING."
                    ),
                    state_file=str(state_file()),
                )
            )
        elif task.session_id not in live_session_ids:
            # session_id set but points to a non-live session (missing from
            # state, TIMED_OUT, etc.).
            # _check_wedge_task_running_completed_session handles COMPLETED.
            # BACKGROUNDED is excluded: the session is intentionally paused
            # and will resume — flagging it as a wedge is a false positive.
            session_by_id_local = {s.id: s for s in state.sessions}
            sess = session_by_id_local.get(task.session_id)
            _non_wedge = {SessionStatus.COMPLETED, SessionStatus.BACKGROUNDED}
            if sess is None or sess.status not in _non_wedge:
                findings.append(
                    WedgeFinding(
                        wedge_class="wedge/task-running-no-session",
                        session_id=task.session_id,
                        ticket_id=task.ticket_id,
                        recipe=(
                            "Queue task RUNNING with no matching session. "
                            "Run: cw doctor --reap to revert task to PENDING."
                        ),
                        state_file=str(state_file()),
                    )
                )
    return findings


def _check_wedge_task_running_completed_session(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect RUNNING queue tasks whose session is already COMPLETED."""
    findings: list[WedgeFinding] = []
    session_by_id = {s.id: s for s in state.sessions}
    for task in queue.tasks:
        if task.status != QueueItemStatus.RUNNING or task.session_id is None:
            continue
        session = session_by_id.get(task.session_id)
        if session is None or session.status != SessionStatus.COMPLETED:
            continue
        findings.append(
            WedgeFinding(
                wedge_class="wedge/task-running-completed-session",
                session_id=task.session_id,
                ticket_id=task.ticket_id,
                recipe=(
                    "Queue task RUNNING but its session is already COMPLETED. "
                    "Run: cw doctor --reap to revert task to PENDING."
                ),
                state_file=str(state_file()),
            )
        )
    return findings


def _resolve_wedge_branch(
    task: TicketTask,
    session_by_id: dict[str, Session],
    clients: dict[str, ClientConfig],
) -> str:
    """Branch for a wedge check: the session's branch, else the feature prefix.

    Falls back to ``<feature_branch_prefix>/<ticket>`` (``dev`` when the client
    is unknown), mirroring what the staged pipeline provisions and pushes (#712).
    """
    if task.session_id is not None:
        session = session_by_id.get(task.session_id)
        if session is not None and session.branch:
            return session.branch
    return feature_branch_key(task.client, task.ticket_id, clients)


def _check_wedge_repo_ahead(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect RUNNING tasks whose branch is pushed to remote but queue not updated.

    Uses ``git ls-remote`` to check if the branch exists on the remote and
    ``gh pr list`` to determine whether a PR is open. Advisory only — no
    automatic reap.
    """
    findings: list[WedgeFinding] = []
    session_by_id = {s.id: s for s in state.sessions}
    # A broken clients.yaml must not crash the doctor run; degrade to no
    # clients and fall back to the default feature-branch prefix below
    # (mirrors the guard around load_clients in run_doctor).
    try:
        clients = load_clients()
    except (OSError, yaml.YAMLError, CwError, ValidationError):
        clients = {}
    for task in queue.tasks:
        if task.status != QueueItemStatus.RUNNING:
            continue
        if task.worktree_path is None:
            continue
        # Branch resolution: prefer session branch, fallback to the client's
        # configured feature branch (<feature_branch_prefix>/<ticket>, e.g.
        # dev/662 — what the staged pipeline provisions and pushes, #712).
        branch = _resolve_wedge_branch(task, session_by_id, clients)
        # Get remote URL from worktree
        try:
            remote_result = _sp.run(
                ["git", "-C", str(task.worktree_path), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=False,
            )
            if remote_result.returncode != 0:
                continue
            remote_url = remote_result.stdout.strip()
        except OSError:
            continue
        # Check if branch exists on remote
        try:
            ls_result = _sp.run(
                ["git", "ls-remote", remote_url, f"refs/heads/{branch}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if ls_result.returncode != 0 or not ls_result.stdout.strip():
                continue
        except (OSError, _sp.TimeoutExpired):
            continue
        # Check PR status via gh CLI
        recipe: str
        prs, _ = _gh_pr_states(branch)
        if not prs:
            recipe = (
                f"Branch {branch} is ahead of main with no open PR. "
                f"Suggested: cw spawn-complete {task.ticket_id} or open PR manually."
            )
        else:
            pr_state = prs[0].get("state", "OPEN")
            recipe = (
                f"Branch {branch} has {pr_state} PR but queue still RUNNING. "
                f"Suggested: cw spawn-complete {task.ticket_id} --status shipped."
            )
        findings.append(
            WedgeFinding(
                wedge_class="wedge/repo-ahead-of-queue",
                session_id=task.session_id,
                ticket_id=task.ticket_id,
                recipe=recipe,
                state_file=str(state_file()),
            )
        )
    return findings


def _is_dead_session_task(
    task: TicketTask,
    session_by_id: dict[str, Session],
    live_short_ids: set[str],
) -> bool:
    """Return True when a BLOCKED_ON_USER task's session is dead.

    Dead = session_id is None (dirty-worktree / gh-blocked phantom paths),
    OR session not in state, OR surface_ref is None or absent from the live
    daemon roster. Covers all three BLOCKED_ON_USER creation paths (see #590).
    """
    if task.session_id is None:
        return True
    session = session_by_id.get(task.session_id)
    if session is None:
        return True
    if session.surface_ref is None:
        return True
    return session.surface_ref not in live_short_ids


def _check_wedge_dead_session_blocked_on_user(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect BLOCKED_ON_USER tasks whose sessions are dead (OOM/crash path).

    Guards daemon I/O: list_live_session_short_ids() is only called when at
    least one BLOCKED_ON_USER task exists in the queue.
    """
    candidates = [t for t in queue.tasks if t.status == QueueItemStatus.BLOCKED_ON_USER]
    if not candidates:
        return []

    session_by_id = {s.id: s for s in state.sessions}
    live_short_ids = get_native_daemon_client().list_live_session_short_ids()

    findings: list[WedgeFinding] = []
    for task in candidates:
        if not _is_dead_session_task(task, session_by_id, live_short_ids):
            continue
        findings.append(
            WedgeFinding(
                wedge_class=_WEDGE_BLOCKED_DEAD_SESSION,
                session_id=task.session_id,
                ticket_id=task.ticket_id,
                recipe=(
                    "BLOCKED_ON_USER task with dead session holds lane slot. "
                    "Run: cw doctor --reap to revert task to PENDING."
                ),
                state_file=str(state_file()),
            )
        )
    return findings


def _daemon_supervisor_alive() -> bool:
    """Return True when roster.json reports a positive supervisorPid.

    Uses the same source as :func:`_check_daemon_reachable` so the outage
    guard is consistent between the health check and the wedge detector.
    """
    try:
        data: dict[str, object] = json.loads(_ROSTER_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    pid = data.get("supervisorPid", 0)
    return isinstance(pid, int) and pid > 0


def _check_wedge_active_no_daemon_entry(
    state: CwState,
) -> list[WedgeFinding]:
    """Detect ACTIVE/IDLE sessions absent from the daemon live roster.

    Guards on a positive supervisorPid before treating an empty live set
    as "sessions are dead" — a missing or zero supervisorPid means the
    daemon is restarting; skipping prevents mass-reap false-positives.

    Uses :func:`compute_drift` to apply the same four guards as reconcile:
    surface_ref present, ref absent from live set, past SPAWN_GRACE_SECONDS,
    purpose != ORCHESTRATE.
    """
    if not _daemon_supervisor_alive():
        return []

    native_live = get_native_daemon_client().list_live_session_short_ids()
    drift = compute_drift(state, native_live)

    session_by_id = {s.id: s for s in state.sessions}
    findings: list[WedgeFinding] = []
    for session_id in drift.phantom_session_ids:
        session = session_by_id.get(session_id)
        ticket_id = ticket_id_for_session(session.name) if session else None
        findings.append(
            WedgeFinding(
                wedge_class=_WEDGE_ACTIVE_NO_DAEMON_ENTRY,
                session_id=session_id,
                ticket_id=ticket_id,
                recipe=(
                    "ACTIVE session has no live daemon entry — session crashed "
                    "without writing a terminal sentinel. "
                    "Run: cw doctor --reap to mark COMPLETED and release the "
                    "hook context lock."
                ),
                state_file=str(state_file()),
            )
        )
    return findings


def _collapse_blocked_on_user_tasks(
    queue: DevQueueStore,
    blocked_ticket_ids: set[str],
) -> bool:
    """Revert oldest BLOCKED_ON_USER task to PENDING; cancel duplicates.

    For each ticket_id in blocked_ticket_ids, sorts BLOCKED_ON_USER tasks by
    created_at (ascending), reverts the first (oldest) to PENDING with
    session_id cleared, and cancels the rest. Skips the whole ticket
    (no mutation) when the oldest task already has ``pr_url`` set — see
    the inline comment at the guard for why.

    Returns True when any mutation was applied.
    """
    changed = False
    for ticket_id in blocked_ticket_ids:
        tasks_for_ticket = [
            t
            for t in queue.tasks
            if t.ticket_id == ticket_id and t.status == QueueItemStatus.BLOCKED_ON_USER
        ]
        if not tasks_for_ticket:
            continue
        # Stable sort preserves insertion order for equal created_at values.
        tasks_for_ticket.sort(key=lambda t: t.created_at)
        oldest = tasks_for_ticket[0]
        # Why: reverting a task that already has a pr_url clears it and
        # re-enables dispatch, which re-runs FINALIZE against a branch that
        # may already be merged — producing a duplicate/empty PR (#912).
        if oldest.pr_url:
            _log.warning(
                "Skipping _collapse_blocked_on_user_tasks for ticket %s: "
                "oldest BLOCKED_ON_USER task has pr_url set (%s). "
                "Will not revert to PENDING.",
                ticket_id,
                oldest.pr_url,
            )
            continue
        transition_task_status(oldest, QueueItemStatus.PENDING)
        oldest.session_id = None
        changed = True
        for dup in tasks_for_ticket[1:]:
            transition_task_status(dup, QueueItemStatus.CANCELLED)
            changed = True
    return changed


def _reap_wedge_findings(findings: list[WedgeFinding]) -> None:
    """Apply mutations for actionable wedge classes.

    Class-2 (task-running-no-session): revert queue task to PENDING.
    Class-3 (task-running-completed-session): revert queue task to PENDING.
    Class-4 (repo-ahead-of-queue): advisory only — no mutations.
    Class-5 (blocked-on-user-dead-session): revert oldest to PENDING, cancel
        duplicates via _collapse_blocked_on_user_tasks — skipped entirely if
        the oldest task already has pr_url set.
    Class-6 (active-no-daemon-entry): call _reap_session_by_selector per
        phantom session; that helper marks COMPLETED, reverts queue task to
        PENDING, stops the daemon surface, and emits an audit event.

    The former class-1 (pane-idle-but-active) wedge was removed with the
    multiplexer substrate — under the native daemon there are no panes to
    inspect for an idle shell (see #504).
    """
    running_ticket_ids: set[str] = {
        f.ticket_id
        for f in findings
        if f.ticket_id
        and f.wedge_class
        not in {
            "wedge/repo-ahead-of-queue",
            _WEDGE_BLOCKED_DEAD_SESSION,
            _WEDGE_ACTIVE_NO_DAEMON_ENTRY,
        }
    }
    blocked_ticket_ids: set[str] = {
        f.ticket_id
        for f in findings
        if f.ticket_id and f.wedge_class == _WEDGE_BLOCKED_DEAD_SESSION
    }
    phantom_session_ids: list[str] = [
        f.session_id
        for f in findings
        if f.session_id and f.wedge_class == _WEDGE_ACTIVE_NO_DAEMON_ENTRY
    ]

    if not running_ticket_ids and not blocked_ticket_ids and not phantom_session_ids:
        return

    with dev_queue_lock():
        queue = load_dev_queue()
        changed = False
        for task in queue.tasks:
            if (
                task.ticket_id in running_ticket_ids
                and task.status == QueueItemStatus.RUNNING
            ):
                transition_task_status(task, QueueItemStatus.PENDING)
                task.session_id = None
                changed = True
        if blocked_ticket_ids:
            blocked_changed = _collapse_blocked_on_user_tasks(queue, blocked_ticket_ids)
            changed = changed or blocked_changed
        if changed:
            save_dev_queue(queue)

    # Reap phantom sessions outside the queue lock — _reap_session_by_selector
    # acquires sessions_lock and dev_queue_lock internally (sequential, no
    # deadlock risk since we already released dev_queue_lock above).
    for session_id in phantom_session_ids:
        _reap_session_by_selector(session_id)


def _check_loop_health() -> list[CheckResult]:
    """Detect dispatch stalls: pending>0, running==0 across N consecutive ticks.

    Reads DISPATCH_TICK events from the last hour, groups by client, and checks
    whether the most recent _LOOP_STALL_CONSECUTIVE_TICKS ticks are all
    FRESHNESS_GATE with pending>0 and running==0. When a stall is detected for
    a client, emits a warn=True result suggesting ``cw dev-queue refresh-all``.

    This is the on-demand forensic replay (threshold
    _LOOP_STALL_CONSECUTIVE_TICKS=3, derived from tick events) and coexists
    with the proactive, persisted runtime latch
    ``ClientConcurrencyOverride.consecutive_freshness_blocks`` (threshold 5,
    RFC 0007 §W2) — the two are deliberately not unified.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    events = read_events(
        event_types=[OrchestratorEventType.DISPATCH_TICK],
        since_ts=cutoff,
    )

    per_client: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        client = ev.payload.get("client", "")
        per_client.setdefault(client, []).append(ev.payload)

    results: list[CheckResult] = []
    for client, ticks in per_client.items():
        recent = ticks[-_LOOP_STALL_CONSECUTIVE_TICKS:]
        if len(recent) < _LOOP_STALL_CONSECUTIVE_TICKS:
            continue
        stalled = all(
            t.get("skip_reason") == DispatchSkipReason.FRESHNESS_GATE
            and int(t.get("pending", 0)) > 0
            and int(t.get("running", 0)) == 0
            and int(t.get("claimed", 0)) == 0
            for t in recent
        )
        if stalled:
            results.append(
                CheckResult(
                    f"loop-health/{client}",
                    ok=True,
                    warn=True,
                    detail=(
                        f"dispatch stalled for {client} — main behind origin."
                        " Run `cw dev-queue refresh-all`."
                    ),
                )
            )

    if not results:
        results.append(
            CheckResult("loop-health", ok=True, warn=False, detail="no stall detected")
        )
    return results


def _check_loop_liveness() -> list[CheckResult]:
    """Warn when any client's last dispatch tick is stale and has pending tickets."""
    tick_data: dict[str, TickSummary] = latest_tick_summary_by_client()
    if not tick_data:
        return [
            CheckResult("loop-liveness", ok=True, warn=False, detail="no tick history")
        ]

    now = datetime.now(UTC)
    results: list[CheckResult] = []
    for client, tick in tick_data.items():
        age = (now - tick.tick_at).total_seconds()
        if age > TICK_STALE_SECONDS and tick.pending > 0:
            results.append(
                CheckResult(
                    f"loop-liveness/{client}",
                    ok=True,
                    warn=True,
                    detail=(
                        f"no dispatch tick for {client} in {int(age)}s"
                        f" ({tick.pending} pending) — loop may have exited."
                        " Run `cw dev-queue run`."
                    ),
                )
            )
    if not results:
        results.append(
            CheckResult(
                "loop-liveness",
                ok=True,
                warn=False,
                detail="no stale+pending condition",
            )
        )
    return results


def _gh_pr_states(branch: str) -> tuple[list[dict[str, Any]], bool]:
    """Return (pr_list, gh_missing) for the given branch.

    Returns ([], False) on empty result or non-zero exit.
    Returns ([], True) if gh binary is not found.
    Swallows OSError, ValueError, TimeoutExpired.
    """
    try:
        pr_result = _sp.run(
            ["gh", "pr", "list", "--head", branch, "--json", "state", "--limit", "1"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        prs: list[dict[str, Any]] = (
            json.loads(pr_result.stdout) if pr_result.returncode == 0 else []
        )
    except FileNotFoundError:
        return [], True
    except (OSError, ValueError, _sp.TimeoutExpired):
        return [], False
    else:
        return prs, False


def _check_timed_out_merged(
    state: CwState,
    clients: dict[str, ClientConfig],
) -> list[CheckResult]:
    """Detect TIMED_OUT sessions whose linked PR has since merged.

    Scans TIMED_OUT DAEMON sessions whose completed_at falls within
    TIMED_OUT_MERGED_LOOKBACK_DAYS, extracts the ticket id from the
    session name, and uses ``gh issue view`` + ``gh pr view`` to
    determine whether a linked PR is MERGED. Emits a warn=True result
    per session when a merged PR is found.

    *clients* is used to resolve each session's
    :attr:`ClientConfig.feature_branch_prefix` (SSOT for the branch name the
    staged pipeline provisions; GitHub #728).
    """
    cutoff = datetime.now(UTC) - timedelta(days=TIMED_OUT_MERGED_LOOKBACK_DAYS)
    results: list[CheckResult] = []
    gh_missing = False

    for session in state.sessions:
        if session.status != SessionStatus.TIMED_OUT:
            continue
        if session.origin != SessionOrigin.DAEMON:
            continue
        if session.completed_at is None or session.completed_at < cutoff:
            continue

        ticket_id = ticket_id_for_session(session.name)
        if ticket_id is None:
            continue

        branch = feature_branch_key(session.client, ticket_id, clients)
        merged, gh_available = pr_is_merged_for_ticket(ticket_id, branch=branch)
        if not gh_available and not gh_missing:
            results.append(
                CheckResult(
                    "timed_out-merged",
                    ok=True,
                    warn=True,
                    detail="gh unavailable; skipping timed_out-merged check",
                )
            )
            gh_missing = True
            continue

        if merged is True:
            results.append(
                CheckResult(
                    f"timed_out-merged/{session.id}",
                    ok=True,
                    warn=True,
                    detail=(
                        f"session {session.id} is TIMED_OUT but linked PR"
                        f" for ticket {ticket_id} is MERGED — see #315."
                    ),
                )
            )

    return results


def _reap_session_by_selector(
    selector: str,
    *,
    authority: str = "operator",
    lane: str | None = None,
    proposed_action: str | None = None,
    correlation_id: str | None = None,
) -> bool:
    """Reap a single session by exact short id or exact session name.

    Bypasses ``reap_policy`` — targeted reap is always authorized by the operator.

    Called directly from the CLI ``doctor --reap <SESSION>`` targeted path.
    Does NOT go through ``run_doctor`` to avoid changing its return type.
    Uses the same write primitives as the normal reconcile act phase.

    Returns True when the session was found (even if already terminal).
    Returns False when no session matches *selector*.
    """
    with sessions_lock():
        state = load_state()
        target = next(
            (s for s in state.sessions if selector in (s.id, s.name)),
            None,
        )
        if target is None:
            return False
        if target.status not in (
            SessionStatus.ACTIVE,
            SessionStatus.IDLE,
            SessionStatus.BACKGROUNDED,
        ):
            # Already terminal — idempotent.
            return True
        now = datetime.now(UTC)
        target.status = SessionStatus.COMPLETED
        target.completed_at = now
        target.completed_reason = CompletionReason.USER
        save_state(state)

    # Stop daemon surface after releasing sessions_lock.
    if target.surface_ref is not None:
        with contextlib.suppress(Exception):
            get_native_daemon_client().stop(target.surface_ref)

    # Revert owning TicketTask to PENDING — separate lock per established pattern.
    # When no RUNNING task exists, also try to collapse BLOCKED_ON_USER duplicates.
    ticket_id = ticket_id_for_session(target.name)
    mutations: list[str] = ["session_status_completed", "daemon_stopped"]
    if ticket_id:
        with dev_queue_lock():
            store = load_dev_queue()
            running_reverted = False
            for task in store.tasks:
                if (
                    task.ticket_id == ticket_id
                    and task.status == QueueItemStatus.RUNNING
                ):
                    transition_task_status(task, QueueItemStatus.PENDING)
                    task.session_id = None
                    running_reverted = True
                    break
            if running_reverted:
                mutations.append("task_reverted_to_pending")
                save_dev_queue(store)
            else:
                # No RUNNING task — try to collapse dead-session BLOCKED_ON_USER rows.
                # Why: liveness is not re-checked here because _reap_session_by_selector
                # targets a specific session; BLOCKED_ON_USER tasks for the same ticket
                # are crash artifacts of that session. A live BLOCKED_ON_USER from a
                # concurrent session is an unusual race; _reap_wedge_findings checks
                # liveness before routing ticket_ids to this helper.
                blocked_changed = _collapse_blocked_on_user_tasks(store, {ticket_id})
                if blocked_changed:
                    mutations.append("blocked_task_reverted_to_pending")
                    save_dev_queue(store)

    # Emit audit event after all locks released. record_event uses _inbox_lock
    # (separate file lock — no deadlock risk). Covers both automated 4c consumer
    # and manual cw doctor --reap so propose→authorize→act is fully traceable.
    record_event(
        OrchestratorEventType.SESSION_REAP_AUTHORIZED,
        payload={
            "session_id": target.id,
            "session_name": target.name,
            "client": target.client,
            "ticket_id": ticket_id,
            "lane": lane,
            "authority": authority,
            "proposed_action": proposed_action,
            "mutations": mutations,
        },
        correlation_id=correlation_id,
    )
    return True


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
        _clients = load_clients()
    except (OSError, yaml.YAMLError, CwError, ValidationError):
        _clients = {}
    report.checks.extend(_check_project_configs(_clients))
    report.checks.extend(_check_review_strategy(_clients))
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
    report.checks.append(_check_codex_capability())
    report.checks.append(_check_daemon_reachable())
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
        queue = load_dev_queue()
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


def _check_bypass_disclaimer() -> CheckResult:
    """Check whether the user has accepted the bypass-permissions disclaimer."""
    try:
        raw = _CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CheckResult(
            "bypass-disclaimer",
            ok=True,
            warn=True,
            detail=f"settings.json not found at {_CLAUDE_SETTINGS_PATH}",
        )
    try:
        data: dict[str, object] = json.loads(raw)
    except json.JSONDecodeError as exc:
        return CheckResult(
            "bypass-disclaimer",
            ok=True,
            warn=True,
            detail=f"could not parse settings.json: {exc}",
        )
    if data.get("skipDangerousModePermissionPrompt"):
        return CheckResult("bypass-disclaimer", ok=True, warn=False, detail="accepted")
    return CheckResult(
        "bypass-disclaimer",
        ok=True,
        warn=True,
        detail=(
            "skipDangerousModePermissionPrompt not set"
            " — run `claude --dangerously-skip-permissions` once interactively"
        ),
    )


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a 'X.Y.Z' version string into a comparable int tuple.

    Returns an empty tuple when the string is absent, too short, or
    non-numeric — callers treat an empty return as "unparseable".
    """
    parts = v.split(".")
    if len(parts) < _VERSION_PARTS:
        return ()
    try:
        return tuple(int(p) for p in parts[:_VERSION_PARTS])
    except ValueError:
        return ()


def _dep_distribution_name(entry: str) -> str:
    """Extract the leading distribution name from a PEP 508 dependency entry.

    Scans for the first separator character (version specifier, environment
    marker, or whitespace) and returns the prefix, stripped. E.g.
    ``"psutil>=6.0"`` → ``"psutil"``, ``"foo; sys_platform=='win32'"`` → ``"foo"``.
    """
    for i, ch in enumerate(entry):
        if ch in _DEP_NAME_SEPARATORS:
            return entry[:i].strip()
    return entry.strip()


def _check_claude_version() -> CheckResult:
    """Check that the claude binary is reachable and return its version.

    Returns ok=True, warn=True when the binary ran but exited non-zero, or when
    the version string cannot be parsed, or when the version is below the floor
    required for native-daemon dispatch.
    """
    try:
        proc = _sp.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError:
        return CheckResult("claude-version", ok=False, detail="claude binary not found")
    except _sp.TimeoutExpired:
        return CheckResult(
            "claude-version", ok=False, detail="claude --version timed out (10s)"
        )

    output = proc.stdout or proc.stderr or ""
    version_line = output.splitlines()[0] if output else ""

    if proc.returncode != 0:
        return CheckResult(
            "claude-version",
            ok=True,
            warn=True,
            detail=f"claude --version exited {proc.returncode}: {version_line}",
        )

    # Parse the leading X.Y.Z token from the version line.
    first_token = version_line.split()[0] if version_line else ""
    parsed = _parse_version(first_token)
    if not parsed:
        return CheckResult(
            "claude-version",
            ok=True,
            warn=True,
            detail=f"could not parse version: {version_line}",
        )

    if parsed < _MIN_CLAUDE_VERSION:
        min_str = ".".join(str(x) for x in _MIN_CLAUDE_VERSION)
        return CheckResult(
            "claude-version",
            ok=True,
            warn=True,
            detail=(
                f"{version_line} — upgrade to >= {min_str} for native-daemon dispatch"
            ),
        )

    return CheckResult("claude-version", ok=True, detail=version_line)


def _check_codex_capability() -> CheckResult:
    """Report codex CLI capability via the shared probe (#1238).

    Thin mapping over ``cw.executor.codex_capability_diagnosis`` — no subprocess
    logic here. Binary absent → FAIL with an install hint; present but
    ``--version`` unconfirmed → WARN with a remediation hint (this diagnosis
    also drives dispatch's pre-spawn capability gate to park codex-backed
    tasks, so the WARN needs an actionable next step, not just the raw
    failure detail); capable → OK with the version line as the diagnostics
    record (the ``detail`` field itself is the persisted diagnostic).
    """
    probe = codex_capability_diagnosis()
    if probe.diagnosis == CODEX_NOT_FOUND:
        return CheckResult(
            "codex-capability",
            ok=False,
            detail=f"{probe.detail} — install via npm install -g @openai/codex",
        )
    if probe.diagnosis == CODEX_VERSION_UNKNOWN:
        return CheckResult(
            "codex-capability",
            ok=True,
            warn=True,
            detail=f"{probe.detail} — re-run `codex --version` manually to diagnose"
            " (PATH, permissions, network)",
        )
    return CheckResult("codex-capability", ok=True, warn=False, detail=probe.detail)


def _resolve_cw_source_path() -> Path | CheckResult:
    """Resolve the local source dir for the installed cw, or a skip CheckResult.

    Returns the source :class:`Path` for an editable/local install. For a
    registry/PyPI install (no package metadata, no/foreign ``direct_url.json``)
    returns an ``ok=True, warn=False`` skip :class:`CheckResult` that the
    caller propagates unchanged.
    """
    try:
        dist = importlib.metadata.distribution(_CW_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=False,
            detail="installed from registry; skipping source check",
        )

    direct_url_text = dist.read_text("direct_url.json")
    if direct_url_text is None:
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=False,
            detail="installed from registry; skipping source check",
        )

    try:
        direct_url: dict[str, object] = json.loads(direct_url_text)
    except json.JSONDecodeError:
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=False,
            detail="malformed direct_url.json; skipping source check",
        )

    url = direct_url.get("url", "")
    if not isinstance(url, str) or not url.startswith("file://"):
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=False,
            detail="installed from registry; skipping source check",
        )

    return Path(urllib.parse.urlparse(url).path)


def _check_cw_version() -> CheckResult:
    """Check whether the installed cw matches the source repo's pyproject.toml version.

    Silent-skips (ok=True, warn=False) for registry/PyPI installs and when
    package metadata is absent — source-version comparison only makes sense
    for local installs. Warns (ok=True, warn=True) when installed is behind
    source or when the source path is stale/unreadable.
    """
    source_path = _resolve_cw_source_path()
    if isinstance(source_path, CheckResult):
        return source_path

    if not source_path.exists():
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=True,
            detail=(
                f"source path {source_path} no longer exists"
                f" — run `{_CW_REINSTALL_CMD}`"
            ),
        )

    pyproject_path = source_path / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as fh:
            pyproject = tomllib.load(fh)
        source_version_str: str = pyproject["project"]["version"]
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError, OSError):
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=True,
            detail=f"could not read source version from {pyproject_path}",
        )

    installed_version_str = importlib.metadata.version(_CW_PACKAGE_NAME)

    installed_ver = _parse_version(installed_version_str)
    source_ver = _parse_version(source_version_str)

    if not installed_ver or not source_ver:
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=True,
            detail=(
                f"could not compare versions:"
                f" installed={installed_version_str} source={source_version_str}"
            ),
        )

    if installed_ver < source_ver:
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=True,
            detail=(
                f"installed {installed_version_str} < source {source_version_str}"
                f" — run `{_CW_REINSTALL_CMD}`"
            ),
        )

    return CheckResult(
        _CW_VERSION_CHECK_NAME,
        ok=True,
        warn=False,
        detail=f"installed {installed_version_str} matches source",
    )


def _check_cw_deps() -> CheckResult:
    """Check whether every dependency declared in source pyproject.toml is installed.

    Detects the class of drift that crash-looped `cw dev-queue serve` on
    2026-07-09 after #1075 added `psutil` to pyproject.toml but the running
    tool venv was never re-synced. Silent-skips (ok=True, warn=False) for
    registry/PyPI installs and when package metadata is absent — this check
    only makes sense for local editable installs. Warns (ok=True, warn=True)
    when the source path is stale, the dependencies list is unreadable or
    malformed, or one or more declared dependencies are not installed.
    """
    source_path = _resolve_cw_source_path()
    if isinstance(source_path, CheckResult):
        return CheckResult(
            _CW_DEPS_CHECK_NAME,
            ok=source_path.ok,
            warn=source_path.warn,
            detail=source_path.detail,
        )

    if not source_path.exists():
        return CheckResult(
            _CW_DEPS_CHECK_NAME,
            ok=True,
            warn=True,
            detail=(
                f"source path {source_path} no longer exists"
                f" — run `{_CW_REINSTALL_CMD}`"
            ),
        )

    pyproject_path = source_path / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as fh:
            pyproject = tomllib.load(fh)
        dependencies = pyproject["project"]["dependencies"]
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError, OSError):
        return CheckResult(
            _CW_DEPS_CHECK_NAME,
            ok=True,
            warn=True,
            detail=f"could not read dependencies from {pyproject_path}",
        )

    if not isinstance(dependencies, list):
        return CheckResult(
            _CW_DEPS_CHECK_NAME,
            ok=True,
            warn=True,
            detail=f"dependencies in {pyproject_path} is not a list",
        )

    missing: list[str] = []
    for entry in dependencies:
        if not isinstance(entry, str):
            continue
        name = _dep_distribution_name(entry)
        try:
            importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)

    if missing:
        return CheckResult(
            _CW_DEPS_CHECK_NAME,
            ok=True,
            warn=True,
            detail=(f"not installed: {', '.join(missing)} — run `{_CW_REINSTALL_CMD}`"),
        )

    return CheckResult(
        _CW_DEPS_CHECK_NAME,
        ok=True,
        warn=False,
        detail=f"{len(dependencies)} declared dependencies all installed",
    )


def _check_daemon_reachable() -> CheckResult:
    """Check whether the Claude native daemon's roster reports a running supervisor."""
    try:
        raw = _ROSTER_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CheckResult(
            "daemon-reachable",
            ok=True,
            warn=True,
            detail=f"roster.json not found at {_ROSTER_PATH} — daemon not started?",
        )
    try:
        data: dict[str, object] = json.loads(raw)
    except json.JSONDecodeError as exc:
        return CheckResult(
            "daemon-reachable",
            ok=True,
            warn=True,
            detail=f"could not parse roster.json: {exc}",
        )
    pid = data.get("supervisorPid", 0)
    if isinstance(pid, int) and pid > 0:
        return CheckResult(
            "daemon-reachable", ok=True, warn=False, detail=f"supervisorPid={pid}"
        )
    return CheckResult(
        "daemon-reachable",
        ok=True,
        warn=True,
        detail="supervisorPid absent or zero — daemon may not be running",
    )


def format_report(report: DoctorReport) -> str:
    """Render a :class:`DoctorReport` as a human-readable block."""
    lines = [f"cw {report.version}"]
    for check in report.checks:
        if check.warn:
            mark = "WARN"
        elif check.ok:
            mark = "OK"
        else:
            mark = "FAIL"
        line = f"  [{mark}] {check.name}"
        if check.detail:
            line += f" — {check.detail}"
        lines.append(line)
    if report.wedge_findings:
        lines.append("")
        lines.append("wedge findings:")
        for wf in report.wedge_findings:
            lines.append(f"  [{wf.wedge_class}] ticket={wf.ticket_id}")
            lines.append(f"    {wf.recipe}")
    lines.append("")
    if not report.ok:
        footer = "status: problems detected"
    elif report.clean:
        footer = "status: healthy"
    else:
        footer = "status: healthy — advisory warnings"
    lines.append(footer)
    return "\n".join(lines)


def format_report_json(report: DoctorReport) -> str:
    """Render a :class:`DoctorReport` as JSON."""
    return json.dumps(
        {
            "version": 1,
            "ok": report.ok,
            "clean": report.clean,
            "checks": [
                {"name": c.name, "ok": c.ok, "warn": c.warn, "detail": c.detail}
                for c in report.checks
            ],
            "wedge_findings": [
                {
                    "wedge_class": f.wedge_class,
                    "session_id": f.session_id,
                    "ticket_id": f.ticket_id,
                    "recipe": f.recipe,
                    "state_file": f.state_file,
                }
                for f in report.wedge_findings
            ],
        },
        indent=2,
    )
