"""Import-surface pins for the four-module split (#2999).

``cw.models.tasks``, ``cw.dispatch.loop``, ``cw.pr_hydrate`` and
``cw.dev_queue.next_commands`` were each near or over the ~1000-line module
ceiling. The split is a pure move, so the behavioural suites stay as they are;
this file pins what those suites cannot see, and it was written against the
unsplit layout first so every section below (bar the moved-to identity pin)
passed before any source moved:

1. Every name importers use, and every top-level name each module defined,
   still resolves on the old path. The tuples are hard-coded, not derived at
   runtime, so a dropped re-export fails loudly.
2. Every ``monkeypatch.setattr`` target in the suite (string form and the
   ``loop_mod`` object form) is still a module attribute of the old path.
3. Every function whose free names are patched at the old path is still
   *defined* there. A function reads its globals from the module that defines
   it, so re-importing a moved consumer would silently decouple the patch (the
   ``cw.dispatch.routing`` trap, pinned the same way in
   ``tests/test_dispatch_routing_package.py``).
4. The ``next_commands`` rule table keeps its 18 entries in first-match order.
5. The ``cw.models.tasks`` JSON schemas are byte-for-byte unchanged against
   ``tests/fixtures/models_tasks_schemas.json``, captured from the unsplit
   module and regenerated (``json.dumps(..., indent=1)`` plus a newline) only
   by a deliberate schema change, such as v55's ``TicketTask.review_debt``
   (#3041). Serialization field order drives the ``dev_queue.json`` layout.
6. Each old path imports cleanly in a cold interpreter.
7. Logger names are unchanged.
"""

from __future__ import annotations

import importlib
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "models_tasks_schemas.json"

_TASKS = "cw.models.tasks"
_LOOP = "cw.dispatch.loop"
_PR_HYDRATE = "cw.pr_hydrate"
_NEXT_COMMANDS = "cw.dev_queue.next_commands"

_OLD_PATH_NAMES: dict[str, tuple[str, ...]] = {
    _TASKS: (
        "TicketTask",
        "DispatchPlan",
        "DevQueueStore",
        "PendingFixDispatch",
        "LaunchedFixWorker",
        "UsageLimitAct",
        "MustFixOverride",
        "ReviewApproverKind",
        "DEFAULT_LANE",
        "DEFAULT_STAGE",
        "DEV_QUEUE_SCHEMA_VERSION",
        "PLAN_DRAFT_FINGERPRINT_KEY",
        "PLAN_APPROVED_FINGERPRINT_KEY",
        "PLAN_PROMOTED_KEY",
        "SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY",
        "SCOPE_DRIFT_APPROVED_HEAD_KEY",
        "MUST_FIX_OVERRIDE_KEY",
        "PARK_ON_ABANDONED_EXIT_KEY",
        "occupies_lane_slot",
        "_SAFE_TICKET_ID",
        "_SAFE_TICKET_BRANCH",
        "_validate_gate_recipe_keys",
        "_validate_review_recipe_keys",
        "_validate_park_on_abandoned_exit_keys",
    ),
    _NEXT_COMMANDS: (
        "HUMAN_GATE_CELL_LABEL",
        "HUMAN_GATE_LABEL",
        "HUMAN_GATE_PURPOSE_LABEL",
        "LANE_PAUSED_SKIP_CODE",
        "LANE_RESUME_PURPOSE",
        "SCOPE_DRIFT_PATHS_PLACEHOLDER",
        "_OFF_PIPELINE_STATUSES",
        "_RULES",
        "compact_command",
        "dev_queue_command",
        "human_gate_attention_command",
        "human_gate_settled_notice",
        "is_human_gate_command",
        "is_human_kept_gate",
        "next_commands",
        "red_recover_command",
        "refusal_suffix",
        "row_carries_human_opt_in",
    ),
    _PR_HYDRATE: (
        "PrAttentionState",
        "WATCHED_PR_COUNTERPARTY",
        "Counterparty",
        "_PR_URL_RE",
        "_REMOTE_SLUG_RE",
        "_blocked_attention_state",
        "_comment_body_is_blocking",
        "_compute_attention_state",
        "_derive_pr_state",
        "_diff_transitions",
        "_has_blocking_comment_review",
        "_hydrate_watched_prs",
        "_is_candidate",
        "_overlay_push_observation",
        "_parse_pr_url",
        "_persist_rearm_owed_pr_state",
        "_resolve_repo_slug",
        "_resolve_task_by_pr_ref",
        "_reviewer_node_login",
        "_slug_mismatch",
        "_throttled",
        "apply_pr_state_observation",
        "derive_counterparty",
        "hydrate_pr_states",
        "observe_pushed_event",
        "repo_slug_mismatch",
        "resolve_and_register_review_request",
        "summarize_status_checks",
    ),
    # Every top-level def and constant ``loop.py`` held before the split, bar
    # ``_reconcile_owns_completion``: it moved to ``session_completions`` with
    # its only caller and nothing imports it from ``loop`` (see
    # ``_LOOP_MOVED_TO``).
    _LOOP: (
        "TICK_STALE_SECONDS",
        "_CW_PACKAGE_NAME",
        "_DISPATCH_CONSUMER",
        "_LOADED_VERSION",
        "_resolve_loaded_version",
        "_apply_events_to_store",
        "consume_completed_sessions",
        "_conflicting_branches_lock_free",
        "read_pending_completions",
        "apply_pending_completions",
        "_apply_pending_completion_events",
        "apply_pending_completion",
        "_merge_persisted_usage_limited_until",
        "_arm_usage_limit_windows",
        "_usage_limit_window_is_active",
        "_emit_usage_limit_cleared",
        "_handle_usage_limit_window_transition",
        "_reload_tick_config",
        "_check_version_drift",
        "run_dispatch_loop",
        "_run_pr_state_hydration_guarded",
        "_run_shipped_red_scan_guarded",
        "_run_pr_state_steps_guarded",
        "_run_stale_dispatch_watch_registration_guarded",
        "_run_stale_gate_release_guarded",
        "_run_stale_client_watchdog_guarded",
        "_warn_if_scoped_serve_starves_siblings",
        "_run_boot_passes",
        "_run_dispatch_loop_body",
    ),
}

# ``monkeypatch.setattr`` targets in the suite. String form
# (``"cw.dispatch.loop.time.sleep"``) resolves the leading attribute on the old
# path; object form (``setattr(loop_mod, "dev_queue_lock", ...)``) needs the
# name to be a module attribute so ``raising=True`` does not fail.
_PATCH_TARGETS: dict[str, tuple[str, ...]] = {
    _LOOP: (
        # string form
        "dispatch_tick",
        "time",
        "importlib",
        "record_event",
        "load_effective_config",
        "hydrate_pr_states",
        "scan_shipped_red_prs",
        "release_stale_gated_tasks",
        "register_stale_dispatch_watched_prs",
        "_notify_stale_clients_with_pending",
        "latest_tick_summary_by_client",
        "merge_and_save_usage_limited_until",
        "dispatch_loop_lock",
        "loop_record_fields",
        "_LOADED_VERSION",
        "_handle_usage_limit_window_transition",
        # object form, via ``import cw.dispatch.loop as loop_mod``
        "dev_queue_lock",
        "apply_pending_completions",
        "resolve_conflicting_branches",
        "load_dev_queue",
        "load_effective_clients",
    ),
    _PR_HYDRATE: (
        "fetch_pr_view",
        "run_git",
        "_resolve_repo_slug",
        "_derive_pr_state",
        "apply_pr_state_observation",
    ),
}

# Functions whose free names are patched at the old path. Each must stay
# defined there, or the patch stops reaching it.
_MONKEYPATCH_COUPLED: dict[str, tuple[str, ...]] = {
    _LOOP: (
        "consume_completed_sessions",
        "read_pending_completions",
        "_conflicting_branches_lock_free",
        "_apply_pending_completion_events",
        "apply_pending_completions",
        "apply_pending_completion",
        "_emit_usage_limit_cleared",
        "_handle_usage_limit_window_transition",
        "_resolve_loaded_version",
        "_reload_tick_config",
        "_check_version_drift",
        "run_dispatch_loop",
        "_run_pr_state_hydration_guarded",
        "_run_shipped_red_scan_guarded",
        "_run_stale_dispatch_watch_registration_guarded",
        "_run_stale_gate_release_guarded",
        "_run_stale_client_watchdog_guarded",
        "_warn_if_scoped_serve_starves_siblings",
        "_run_boot_passes",
        "_run_dispatch_loop_body",
    ),
    _PR_HYDRATE: (
        "_resolve_repo_slug",
        "repo_slug_mismatch",
        "_derive_pr_state",
        "_hydrate_watched_prs",
        "apply_pr_state_observation",
        "_persist_and_emit",
        "observe_pushed_event",
        "hydrate_pr_states",
    ),
}

# ``(matches, commands)`` function names of ``_RULES``, in first-match order.
_EXPECTED_RULES = (
    ("is_stale_tip_park", "_generic_commands"),
    ("is_approval_park", "_finalize_approval_park_commands"),
    ("_review_approval_gate_parked", "_finalize_approval_park_commands"),
    ("_human_gate_off_gate", "_human_gate_off_gate_commands"),
    ("_human_gate", "_human_gate_commands"),
    ("_pending", "_pending_commands"),
    ("_running", "_running_commands"),
    ("_red_shipped", "_red_shipped_commands"),
    ("_terminal", "_terminal_commands"),
    ("_scope_drift", "_scope_drift_commands"),
    ("_must_fix_overridden", "_must_fix_overridden_commands"),
    ("_must_fix", "_must_fix_commands"),
    ("_attempt_capped", "_attempt_capped_commands"),
    ("_salvage_parked", "_salvage_commands"),
    ("_drain_held", "_drain_commands"),
    ("_at_approval_gate", "_gate_commands"),
    ("_sessionless", "_sessionless_commands"),
    ("_anything", "_generic_commands"),
)

# Models whose validation-mode schema the golden pins, in fixture order.
_SCHEMA_MODELS = (
    "TicketTask",
    "DispatchPlan",
    "DevQueueStore",
    "PendingFixDispatch",
    "LaunchedFixWorker",
    "UsageLimitAct",
    "MustFixOverride",
)

# The two persisted models whose serialization-mode schema is pinned.
_DUMPED_MODELS = ("TicketTask", "DevQueueStore")

# Modules each cold interpreter imports first. Each split phase appends its new
# submodules, so a sibling importing the package ``__init__`` (a cycle that
# only fails in one import order) is caught.
_COLD_IMPORT_FIRST = (
    _TASKS,
    _LOOP,
    _PR_HYDRATE,
    _NEXT_COMMANDS,
    "cw.models.tasks.keys",
    "cw.models.tasks.records",
    "cw.models.tasks.schema",
    "cw.models.tasks.store",
    "cw.models.tasks.ticket_task",
    "cw.models.tasks.validators",
    "cw.dispatch.session_completions",
    "cw.dispatch.usage_limit_windows",
    "cw.pr_hydrate.attention",
    "cw.pr_hydrate.push_overlay",
    "cw.pr_hydrate.repo_slug",
    "cw.pr_hydrate.transitions",
    "cw.pr_hydrate.watched",
    "cw.dev_queue.next_commands._common",
    "cw.dev_queue.next_commands.engine",
    "cw.dev_queue.next_commands.parked",
    "cw.dev_queue.next_commands.running",
    "cw.dev_queue.next_commands.table",
    "cw.dev_queue.next_commands.terminal",
)

# Names moved out of ``cw.dispatch.loop`` into a flat sibling, and the sibling.
# ``loop`` imports each back, so the old path is the same object.
_LOOP_MOVED_TO = (
    ("_reconcile_owns_completion", "cw.dispatch.session_completions"),
    ("_apply_events_to_store", "cw.dispatch.session_completions"),
    ("_merge_persisted_usage_limited_until", "cw.dispatch.usage_limit_windows"),
    ("_arm_usage_limit_windows", "cw.dispatch.usage_limit_windows"),
    ("_usage_limit_window_is_active", "cw.dispatch.usage_limit_windows"),
)

_SRC = Path(__file__).resolve().parent.parent / "src"

# Every source file the split created. None may log under its own
# ``__name__``: a moved logging call keeps the pre-split logger name.
_NEW_SUBMODULE_FILES = (
    "cw/models/tasks/__init__.py",
    "cw/models/tasks/keys.py",
    "cw/models/tasks/records.py",
    "cw/models/tasks/schema.py",
    "cw/models/tasks/store.py",
    "cw/models/tasks/ticket_task.py",
    "cw/models/tasks/validators.py",
    "cw/dispatch/session_completions.py",
    "cw/dispatch/usage_limit_windows.py",
    "cw/pr_hydrate/attention.py",
    "cw/pr_hydrate/push_overlay.py",
    "cw/pr_hydrate/repo_slug.py",
    "cw/pr_hydrate/transitions.py",
    "cw/pr_hydrate/watched.py",
    "cw/dev_queue/next_commands/__init__.py",
    "cw/dev_queue/next_commands/_common.py",
    "cw/dev_queue/next_commands/engine.py",
    "cw/dev_queue/next_commands/parked.py",
    "cw/dev_queue/next_commands/running.py",
    "cw/dev_queue/next_commands/table.py",
    "cw/dev_queue/next_commands/terminal.py",
)


def _golden() -> dict[str, dict[str, object]]:
    data: dict[str, dict[str, object]] = json.loads(
        _GOLDEN_PATH.read_text(encoding="utf-8")
    )
    return data


def _model(name: str) -> type[BaseModel]:
    model = getattr(importlib.import_module(_TASKS), name)
    assert isinstance(model, type)
    assert issubclass(model, BaseModel)
    return model


def _pairs(table: dict[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    return [(mod, name) for mod, names in table.items() for name in names]


class TestOldPathNames:
    """``from <old path> import X`` keeps working for every X."""

    def test_loop_tuple_is_complete(self) -> None:
        assert len(_OLD_PATH_NAMES[_LOOP]) == len(set(_OLD_PATH_NAMES[_LOOP])) == 29

    @pytest.mark.parametrize(("module", "name"), _pairs(_OLD_PATH_NAMES))
    def test_name_resolves_on_old_path(self, module: str, name: str) -> None:
        assert hasattr(importlib.import_module(module), name)


class TestPatchTargets:
    """Every suite ``monkeypatch.setattr`` target is still an old-path attribute."""

    @pytest.mark.parametrize(("module", "name"), _pairs(_PATCH_TARGETS))
    def test_patch_target_is_module_attribute(self, module: str, name: str) -> None:
        assert name in vars(importlib.import_module(module))


class TestDefinedIn:
    """Consumers of patched names are defined on the old path, not re-imported."""

    @pytest.mark.parametrize(("module", "name"), _pairs(_MONKEYPATCH_COUPLED))
    def test_consumer_is_defined_on_old_path(self, module: str, name: str) -> None:
        obj = getattr(importlib.import_module(module), name)

        assert obj.__module__ == module


class TestLoopMovedTo:
    """The seams moved out of ``loop`` live in their flat sibling."""

    @pytest.mark.parametrize(("name", "sibling"), _LOOP_MOVED_TO)
    def test_moved_name_is_defined_in_sibling(self, name: str, sibling: str) -> None:
        obj = getattr(importlib.import_module(sibling), name)

        assert obj.__module__ == sibling

    @pytest.mark.parametrize(
        ("name", "sibling"),
        [pair for pair in _LOOP_MOVED_TO if pair[0] != "_reconcile_owns_completion"],
    )
    def test_old_path_is_the_same_object(self, name: str, sibling: str) -> None:
        moved = getattr(importlib.import_module(sibling), name)

        assert getattr(importlib.import_module(_LOOP), name) is moved


class TestRulesOrder:
    """First match wins, so the rule table order is behaviour."""

    def test_rules_order_is_unchanged(self) -> None:
        rules = importlib.import_module(_NEXT_COMMANDS)._RULES

        assert [(r.matches.__name__, r.commands.__name__) for r in rules] == list(
            _EXPECTED_RULES
        )


class TestSchemaGolden:
    """The ``cw.models.tasks`` schemas match the pinned golden exactly."""

    def test_golden_layout(self) -> None:
        golden = _golden()

        assert list(golden) == ["validation", "serialization"]
        assert list(golden["validation"]) == list(_SCHEMA_MODELS)
        assert list(golden["serialization"]) == list(_DUMPED_MODELS)

    @pytest.mark.parametrize("name", _SCHEMA_MODELS)
    def test_validation_schema_unchanged(self, name: str) -> None:
        schema = _model(name).model_json_schema()

        assert json.dumps(schema) == json.dumps(_golden()["validation"][name])

    @pytest.mark.parametrize("name", _DUMPED_MODELS)
    def test_serialization_schema_unchanged(self, name: str) -> None:
        schema = _model(name).model_json_schema(mode="serialization")

        assert json.dumps(schema) == json.dumps(_golden()["serialization"][name])


class TestLoggerNames:
    """Operators filter on these logger names; the split must not rename them."""

    @pytest.mark.parametrize("module", [_PR_HYDRATE, "cw.pr_hydrate.watched"])
    def test_pr_hydrate_logger_name(self, module: str) -> None:
        logger = importlib.import_module(module).logger

        assert isinstance(logger, logging.Logger)
        assert logger.name == "cw.pr_hydrate"

    @pytest.mark.parametrize(
        "module",
        [_LOOP, "cw.dispatch.session_completions", "cw.dispatch.usage_limit_windows"],
    )
    def test_dispatch_logger_name(self, module: str) -> None:
        log = importlib.import_module(module)._log

        assert isinstance(log, logging.Logger)
        assert log.name == "cw.dispatch"

    @pytest.mark.parametrize("relpath", _NEW_SUBMODULE_FILES)
    def test_no_new_submodule_logs_under_its_own_name(self, relpath: str) -> None:
        assert "getLogger(__name__)" not in (_SRC / relpath).read_text(encoding="utf-8")


@pytest.mark.parametrize("first", _COLD_IMPORT_FIRST)
def test_old_paths_import_cleanly_whichever_module_loads_first(first: str) -> None:
    """Each interpreter starts cold and imports *first* before anything else.

    Uses ``sys.executable`` (not a bare ``python3``) per PYTHON-PATTERNS'
    compiled-dependency isolation rule: ``pydantic_core`` is ABI-bound to this
    venv.
    """
    script = (
        f"import {first}\n"
        "import cw.models\n"
        f"import {_TASKS}\n"
        f"import {_LOOP}\n"
        f"import {_PR_HYDRATE}\n"
        f"import {_NEXT_COMMANDS}\n"
        f"from {_TASKS} import DevQueueStore\n"
        "DevQueueStore()\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True)
