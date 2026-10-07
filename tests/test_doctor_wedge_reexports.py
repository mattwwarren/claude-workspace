"""Re-export, logger and patch-ownership guards for ``cw.doctor.wedge`` (#2164).

The flat ``doctor/wedge.py`` -> ``doctor/wedge/`` package split must keep
every ``from cw.doctor.wedge import X`` call site working unchanged (the
``cw.doctor.core`` and ``cw.doctor`` imports plus ``loop_health``'s deferred
one), keep the emitted logger name ``cw.doctor.wedge`` byte-identical, and keep
every test monkeypatch landing in the namespace the code under test actually
reads. Mirrors ``tests/test_cli_stop_hook_reexports.py`` (#2496): the surface
is asserted against an exhaustive hardcoded set, deliberately NOT re-derived
from the package, so a dropped or renamed name is a falsifiable failure rather
than a tautology. A deliberate addition updates this set in the same commit.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
from pathlib import Path

import pytest

import cw.doctor
import cw.doctor.core
from cw.doctor import wedge
from cw.models import DevQueueStore, QueueItemStatus
from tests.conftest import _make_ticket_task

_PKG = "cw.doctor.wedge"

# The nine module-level constants the flat module bound at top level.
_CONSTANTS = {
    "_DIRTY_WORKTREE_DISPOSITION",
    "_HUMAN_GATED_PARK_DISPOSITIONS",
    "_REAP_CHECK_NAME",
    "_WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL",
    "_WEDGE_ACTIVE_NO_DAEMON_ENTRY",
    "_WEDGE_ACTIVE_NULL_LIVENESS_ORPHAN",
    "_WEDGE_BLOCKED_DEAD_SESSION",
    "_WEDGE_LEAKED_DAEMON_WORKER",
    "_WEDGE_TERMINAL_SIBLING",
}

# The 23 functions the flat module defined.
_FUNCTIONS = {
    "_cancel_terminal_sibling_parks",
    "_check_wedge_active_daemon_stale_no_sentinel",
    "_check_wedge_active_no_daemon_entry",
    "_check_wedge_active_null_liveness_orphan",
    "_check_wedge_dead_session_blocked_on_user",
    "_check_wedge_leaked_daemon_worker",
    "_check_wedge_repo_ahead",
    "_check_wedge_task_running_completed_session",
    "_check_wedge_task_running_no_session",
    "_check_wedge_terminal_sibling_park",
    "_collapse_blocked_on_user_tasks",
    "_daemon_supervisor_alive",
    "_is_dead_session_task",
    "_is_null_liveness_candidate",
    "_is_terminal_sibling_disposition",
    "_is_terminal_sibling_park",
    "_null_liveness_orphan_recipe",
    "_reap_daemon_sessions",
    "_reap_sessions_and_sweep",
    "_reap_timeout_check",
    "_reap_wedge_findings",
    "_resolve_backend_for_orphan_check",
    "_resolve_wedge_branch",
}

# The complete importable surface: every project-defined top-level name the
# flat ``doctor/wedge.py`` bound before the split (23 defs, nine constants and
# ``_log``). Third-party names the old module merely imported
# (``get_native_daemon_client``, ``run_git``, ...) are deliberately NOT part of
# the surface -- see ``TestPatchOwnership``.
EXPECTED_EXPORTS = {*_FUNCTIONS, *_CONSTANTS, "_log"}

# Owning module for each of the 32 defs and constants. Each extraction commit
# of the split edits only the rows it moves.
EXPECTED_OWNER: dict[str, str] = {
    "_DIRTY_WORKTREE_DISPOSITION": _PKG,
    "_HUMAN_GATED_PARK_DISPOSITIONS": _PKG,
    "_WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL": _PKG,
    "_WEDGE_ACTIVE_NO_DAEMON_ENTRY": _PKG,
    "_WEDGE_ACTIVE_NULL_LIVENESS_ORPHAN": _PKG,
    "_WEDGE_BLOCKED_DEAD_SESSION": _PKG,
    "_WEDGE_LEAKED_DAEMON_WORKER": _PKG,
    "_WEDGE_TERMINAL_SIBLING": _PKG,
    "_check_wedge_task_running_no_session": _PKG,
    "_check_wedge_task_running_completed_session": _PKG,
    "_resolve_wedge_branch": _PKG,
    "_check_wedge_repo_ahead": _PKG,
    "_is_dead_session_task": _PKG,
    "_is_terminal_sibling_disposition": _PKG,
    "_check_wedge_dead_session_blocked_on_user": _PKG,
    "_is_terminal_sibling_park": _PKG,
    "_check_wedge_terminal_sibling_park": _PKG,
    "_collapse_blocked_on_user_tasks": _PKG,
    "_cancel_terminal_sibling_parks": _PKG,
    "_daemon_supervisor_alive": _PKG,
    "_check_wedge_active_no_daemon_entry": _PKG,
    "_check_wedge_active_daemon_stale_no_sentinel": _PKG,
    "_resolve_backend_for_orphan_check": _PKG,
    "_is_null_liveness_candidate": _PKG,
    "_null_liveness_orphan_recipe": _PKG,
    "_check_wedge_active_null_liveness_orphan": _PKG,
    "_check_wedge_leaked_daemon_worker": _PKG,
    "_REAP_CHECK_NAME": _PKG,
    "_reap_daemon_sessions": _PKG,
    "_reap_timeout_check": _PKG,
    "_reap_sessions_and_sweep": _PKG,
    "_reap_wedge_findings": _PKG,
}

# The 10 names ``cw.doctor.core`` imports and the four ``cw.doctor`` imports.
_CORE_IMPORTS = (
    "_check_wedge_active_daemon_stale_no_sentinel",
    "_check_wedge_active_no_daemon_entry",
    "_check_wedge_active_null_liveness_orphan",
    "_check_wedge_dead_session_blocked_on_user",
    "_check_wedge_leaked_daemon_worker",
    "_check_wedge_repo_ahead",
    "_check_wedge_task_running_completed_session",
    "_check_wedge_task_running_no_session",
    "_check_wedge_terminal_sibling_park",
    "_reap_wedge_findings",
)
_DOCTOR_IMPORTS = (
    "_check_wedge_repo_ahead",
    "_check_wedge_task_running_completed_session",
    "_check_wedge_task_running_no_session",
    "_reap_wedge_findings",
)


def _project_defined_names() -> set[str]:
    """Top-level functions the package binds that it (or a submodule) defined."""
    names: set[str] = set()
    for name, value in vars(wedge).items():
        if not inspect.isfunction(value):
            continue
        owner = value.__module__
        if owner == _PKG or owner.startswith(f"{_PKG}."):
            names.add(name)
    return names


class TestPackageExportCompleteness:
    """Guards that ``cw.doctor.wedge`` keeps its full pre-split surface."""

    def test_expected_surface_size(self) -> None:
        assert len(_FUNCTIONS) == 23
        assert len(_CONSTANTS) == 9
        assert len(EXPECTED_EXPORTS) == 33

    def test_project_defined_names_match_surface(self) -> None:
        """Every def bound on the package is in the surface, and vice versa."""
        assert _project_defined_names() | _CONSTANTS | {"_log"} == EXPECTED_EXPORTS

    def test_every_expected_name_is_bound(self) -> None:
        """A dropped re-export must fail here, not at a downstream import site."""
        missing = [name for name in EXPECTED_EXPORTS if not hasattr(wedge, name)]
        assert missing == []

    def test_owner_table_covers_the_surface(self) -> None:
        assert set(EXPECTED_OWNER) == EXPECTED_EXPORTS - {"_log"}


class TestOwnership:
    """Each def and constant lives in the module the owner table names."""

    @pytest.mark.parametrize("name", sorted(_FUNCTIONS))
    def test_function_is_defined_in_its_owner(self, name: str) -> None:
        obj = getattr(wedge, name)
        assert obj.__module__ == EXPECTED_OWNER[name]
        assert vars(importlib.import_module(EXPECTED_OWNER[name]))[name] is obj

    @pytest.mark.parametrize("name", sorted(_CONSTANTS))
    def test_constant_is_bound_in_its_owner(self, name: str) -> None:
        owner_namespace = vars(importlib.import_module(EXPECTED_OWNER[name]))
        assert name in owner_namespace
        assert owner_namespace[name] is getattr(wedge, name)


class TestConsumerContract:
    """The package's in-tree importers keep binding the same objects."""

    @pytest.mark.parametrize("name", _CORE_IMPORTS)
    def test_doctor_core_binds_same_object(self, name: str) -> None:
        assert vars(cw.doctor.core)[name] is getattr(wedge, name)

    @pytest.mark.parametrize("name", _DOCTOR_IMPORTS)
    def test_doctor_package_binds_same_object(self, name: str) -> None:
        assert vars(cw.doctor)[name] is getattr(wedge, name)

    def test_loop_health_deferred_import_resolves(self) -> None:
        """``loop_health``'s function-local ``from cw.doctor.wedge import`` holds.

        The deferred import breaks the loop_health <-> wedge cycle; it must
        stay a package-level import that the re-exporting ``__init__`` serves.
        """
        loop_health = importlib.import_module("cw.doctor.loop_health")
        tree = ast.parse(Path(inspect.getfile(loop_health)).read_text("utf-8"))
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == _PKG
            for alias in node.names
        ]
        assert imported == ["_collapse_blocked_on_user_tasks"]
        assert all(hasattr(wedge, name) for name in imported)

    def test_reap_check_name(self) -> None:
        assert wedge._REAP_CHECK_NAME == "wedge-reap"


# (function, module global it reads, module whose namespace it reads it from).
# A test that monkeypatches the global must target that module: a patch on any
# other namespace that also binds the name resolves fine but silently stops
# intercepting. One row per reading function; each extraction commit repoints
# the rows whose function it moves.
PATCH_OWNERSHIP = [
    ("_check_wedge_repo_ahead", "run_git", _PKG),
    ("_check_wedge_dead_session_blocked_on_user", "get_native_daemon_client", _PKG),
    ("_check_wedge_active_no_daemon_entry", "get_native_daemon_client", _PKG),
    (
        "_check_wedge_active_daemon_stale_no_sentinel",
        "get_native_daemon_client",
        _PKG,
    ),
    ("_check_wedge_leaked_daemon_worker", "get_native_daemon_client", _PKG),
    ("_reap_sessions_and_sweep", "get_native_daemon_client", _PKG),
    ("_daemon_supervisor_alive", "_ROSTER_PATH", _PKG),
    (
        "_check_wedge_active_daemon_stale_no_sentinel",
        "load_orchestrator_config",
        _PKG,
    ),
    ("_reap_sessions_and_sweep", "reap_routed_result_findings", _PKG),
    ("_reap_daemon_sessions", "_reap_session_by_selector", _PKG),
    ("_reap_sessions_and_sweep", "sweep_leaked_daemon_workers", _PKG),
]


class TestPatchOwnership:
    """Guards that each patched global lives where its reader looks it up."""

    def test_one_row_per_reader(self) -> None:
        assert len(PATCH_OWNERSHIP) == 11
        assert len(set(PATCH_OWNERSHIP)) == len(PATCH_OWNERSHIP)

    @pytest.mark.parametrize(("function", "global_name", "owner"), PATCH_OWNERSHIP)
    def test_function_reads_global_from_owner(
        self, function: str, global_name: str, owner: str
    ) -> None:
        namespace = vars(importlib.import_module(owner))
        assert getattr(wedge, function).__globals__ is namespace
        assert global_name in namespace


# Every record the package emits must carry the pre-split logger name
# ``cw.doctor.wedge`` verbatim. ``caplog.at_level(..., logger=...)`` alone
# cannot catch a rename -- level inheritance and propagation make a
# ``__name__``-derived child logger (``cw.doctor.wedge.blocked_on_user``) pass
# the same assertions -- so these tests pin ``record.name`` exactly, filtered to
# the record the function under test emits. One case per pre-split ``_log.``
# call site (two, both in ``_collapse_blocked_on_user_tasks``).
PINNED_LOGGER_NAME = "cw.doctor.wedge"


def _names_of(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    return [r.name for r in caplog.records if needle in r.getMessage()]


class TestLoggerNamePinned:
    """Guards that the package split did not rename the emitted logger."""

    def test_collapse_human_gated_warning_name(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A human-gated park left untouched by the collapse logs a warning."""
        disposition = sorted(wedge._HUMAN_GATED_PARK_DISPOSITIONS)[0]
        task = _make_ticket_task(
            ticket_id="T-pin-gated",
            status=QueueItemStatus.BLOCKED_ON_USER,
            disposition=disposition,
        )
        queue = DevQueueStore(tasks=[task])

        with caplog.at_level(logging.DEBUG, logger=PINNED_LOGGER_NAME):
            changed = wedge._collapse_blocked_on_user_tasks(queue, {"T-pin-gated"})

        assert changed is False
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert _names_of(caplog, "left untouched by collapse") == [PINNED_LOGGER_NAME]

    def test_collapse_pr_url_skip_warning_name(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An oldest blocked row carrying ``pr_url`` is skipped with a warning."""
        task = _make_ticket_task(
            ticket_id="T-pin-pr",
            status=QueueItemStatus.BLOCKED_ON_USER,
            pr_url="https://github.com/o/r/pull/9",
        )
        queue = DevQueueStore(tasks=[task])

        with caplog.at_level(logging.DEBUG, logger=PINNED_LOGGER_NAME):
            changed = wedge._collapse_blocked_on_user_tasks(queue, {"T-pin-pr"})

        assert changed is False
        assert task.status == QueueItemStatus.BLOCKED_ON_USER
        assert _names_of(caplog, "has pr_url set") == [PINNED_LOGGER_NAME]

    def test_package_logger_uses_pinned_name(self) -> None:
        assert wedge._log is logging.getLogger(PINNED_LOGGER_NAME)
