"""Config-file and project-config health checks for ``cw doctor``.

Covers clients.yaml / orchestrator.yaml / sessions.json / dev_queue.json
parseability, per-client ``.claude/project-config.yaml`` tracker and
review-strategy validation, the #1201 review-recipe liveness / attention-state
census anomaly layer, and the events inbox size warning.
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from cw.config import (
    clients_file,
    load_orchestrator_config,
    load_state,
    orchestrator_config_file,
    state_file,
)
from cw.doctor import _deps
from cw.doctor._shared import CheckResult
from cw.events import inbox_path
from cw.exceptions import CwError
from cw.models import OrchestratorConfig
from cw.pr_hydrate import _is_candidate
from cw.reconcile.review_recipes import (
    RECIPE_ATTENTION_STATES,
    RECIPE_FIRED_AT_GETTERS,
    resolve_review_recipe_enabled,
)
from cw.review_strategy import HANDLE_KEY_BY_MODE, RECOGNIZED_MODES
from cw.tracker import PROJECT_CONFIG_RELPATH, load_project_config_dict

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, CwState, TicketTask


# Tracker systems cw recognizes in .claude/project-config.yaml. Anything else
# is a config error: the headless worker would silently fall back to its
# built-in default (Linear MCP) and stall on OAuth (see #675 / project-config).
_RECOGNIZED_TRACKERS: frozenset[str] = frozenset({"github-issues", "linear"})


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
        tasks = _deps.load_dev_queue().tasks
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
        tasks = _deps.load_dev_queue().tasks
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
        _deps.load_clients()
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
        _deps.load_dev_queue()
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
