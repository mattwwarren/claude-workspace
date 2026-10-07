"""Re-export, logger and patch-ownership guards for ``cw.cli.stop_hook`` (#2496).

The flat ``cli/stop_hook.py`` -> ``cli/stop_hook/`` package split must keep
every ``from cw.cli.stop_hook import X`` call site working unchanged, keep the
emitted logger name ``cw.cli.stop_hook`` byte-identical, keep the
``signal-stop`` click command registered on ``import cw.cli``, and keep every
test monkeypatch landing in the namespace the code under test actually reads.
This mirrors ``tests/test_reconcile_shared_reexports.py`` (the #2214 split):
the surface is asserted against an exhaustive hardcoded set, deliberately NOT
re-derived from the package, so a dropped or renamed name is a falsifiable
failure rather than a tautology. A deliberate addition updates this set in the
same commit.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import TYPE_CHECKING

import click
import pytest

from cw.auto_dev_result import AutoDevResult
from cw.cli import stop_hook
from cw.models import AGENT_SPAWN_STAMP_KEY, AGENT_SPAWN_UNRESOLVED_COUNT_KEY
from tests._reconcile_helpers import _stage_complete_payload
from tests.conftest import (
    _invoke_hook_command,
    _make_daemon_session,
    _write_hook_context_file,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.native_daemon import FakeNativeDaemonClient

_PKG = "cw.cli.stop_hook"

# The four reason/key constants the flat module bound at top level.
_CONSTANTS = {
    "_SENTINEL_UNROUTABLE_PAGED_KEY",
    "_SENTINEL_UNROUTABLE_REASON",
    "_STAGED_ROUTE_RESCUED_KEY",
    "_STAGED_ROUTE_TASK_ALREADY_TERMINAL_KEY",
}

# The complete importable surface: every project-defined top-level name the
# flat ``cli/stop_hook.py`` bound before the split (30 defs and classes, the
# four constants, and ``logger``). Third-party/stdlib names the old module
# merely imported (``get_native_daemon_client``, ``load_state``, ...) are
# deliberately NOT part of the surface -- see
# ``TestPatchOwnership.test_package_binds_no_patched_global``.
EXPECTED_EXPORTS = {
    # Defs and classes (30)
    "_HeadlessResolution",
    "_LockedStop",
    "_agent_spawn_stamp_is_clear",
    "_armed_running_task",
    "_build_completed_payload",
    "_clear_agent_spawn_stamp",
    "_clear_staged_emit_result_marker",
    "_handle_headless_no_sentinel",
    "_handle_unrouted_stop",
    "_handle_user_origin_stop",
    "_harvest_last_result_through_door",
    "_maybe_clear_staged_emit_result",
    "_maybe_stamp_sentinel_unroutable_paged",
    "_page_sentinel_unroutable",
    "_park_if_abandoned",
    "_parse_headless_sentinel",
    "_peek_staged_emit_result",
    "_read_stop_hook_payload",
    "_reconstruct_emitted_sentinel",
    "_resolve_and_complete_headless_session",
    "_resolve_signal_stop_context",
    "_resolve_stop_under_lock",
    "_restore_staged_route_outcome",
    "_sentinel_frame_follows_marker",
    "_sentinel_unroutable",
    "_sentinel_unroutable_already_paged",
    "_snapshot_agent_spawn_stamp",
    "_stamp_staged_route_outcome",
    "_verify_headless_scope",
    "signal_stop",
    # Constants (4)
    *_CONSTANTS,
    # The module logger
    "logger",
}


def _defining_module(value: object) -> str | None:
    """The module that defined *value*, or ``None`` for a non-definition.

    A click command's own ``__module__`` is ``click.core``; its defining module
    is that of the callback underneath ``handle_errors``.
    """
    if isinstance(value, click.Command) and value.callback is not None:
        return inspect.unwrap(value.callback).__module__
    if inspect.isfunction(value) or inspect.isclass(value):
        return value.__module__
    return None


def _project_defined_names() -> set[str]:
    """Top-level functions/classes/commands the package binds that it defined."""
    names: set[str] = set()
    for name, value in vars(stop_hook).items():
        owner = _defining_module(value)
        if owner is not None and (owner == _PKG or owner.startswith(f"{_PKG}.")):
            names.add(name)
    return names


def _isort_style_key(name: str) -> tuple[int, str]:
    stripped = name.lstrip("_")
    if stripped.isupper():
        return 0, name
    if stripped[:1].isupper():
        return 1, name
    return 2, name


class TestPackageExportCompleteness:
    """Guards that ``cw.cli.stop_hook`` keeps its full pre-split surface."""

    def test_expected_surface_size(self) -> None:
        assert len(EXPECTED_EXPORTS) == 35

    def test_project_defined_names_match_surface(self) -> None:
        """Every def/class/command bound on the package is in the surface."""
        assert _project_defined_names() | _CONSTANTS | {"logger"} == EXPECTED_EXPORTS

    def test_every_expected_name_is_bound(self) -> None:
        """A dropped re-export must fail here, not at a downstream import site."""
        missing = [name for name in EXPECTED_EXPORTS if not hasattr(stop_hook, name)]
        assert missing == []

    def test_all_matches_full_surface(self) -> None:
        assert set(stop_hook.__all__) == EXPECTED_EXPORTS

    def test_all_is_sorted_without_duplicates(self) -> None:
        """Ruff RUF022's isort-style order: SCREAMING_CASE, CamelCase, the rest."""
        assert list(stop_hook.__all__) == sorted(
            set(stop_hook.__all__), key=_isort_style_key
        )


class TestCommandRegistration:
    """``signal-stop`` stays registered on ``main`` by ``import cw.cli`` (#2496)."""

    def test_signal_stop_registered_on_main(self) -> None:
        import cw.cli

        command = cw.cli.main.commands.get("signal-stop")
        assert command is stop_hook.signal_stop

    def test_signal_stop_empty_payload_exits_zero(self, tmp_config_dir: Path) -> None:
        """The hook contract: a payload with no ``cwd`` is a silent no-op."""
        result = _invoke_hook_command("signal-stop", {})
        assert result.exit_code == 0, result.output
        assert result.output == ""

    def test_signal_stop_without_session_exits_zero(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A context whose session is not in state is a silent no-op too."""
        worktree = tmp_path / "wt-contract"
        worktree.mkdir()
        _write_hook_context_file(worktree)

        result = _invoke_hook_command(
            "signal-stop",
            {
                "session_id": "claude-uuid",
                "cwd": str(worktree),
                "hook_event_name": "Stop",
            },
        )

        assert result.exit_code == 0, result.output


# (function, module global it reads, module whose namespace it reads it from).
# A test that monkeypatches the global must target that module: a patch on any
# other namespace that also binds the name resolves fine but silently stops
# intercepting -- and a no-op assertion like ``== []`` or the deferral path's
# ``load_state``/``sessions_lock`` stubs then pass vacuously. Each extraction
# commit repoints the rows whose function it moves. ``_write_cw_context_locked``
# is read at four call sites in three functions, one row per reader.
PATCH_OWNERSHIP = [
    ("_resolve_signal_stop_context", "_read_cw_context", f"{_PKG}.payload"),
    (
        "_parse_headless_sentinel",
        "_parse_sentinel_from_transcript",
        f"{_PKG}.sentinel",
    ),
    ("_harvest_last_result_through_door", "emit_result_locked", f"{_PKG}.sentinel"),
    ("_sentinel_frame_follows_marker", "claude_project_dir", f"{_PKG}.park"),
    ("_park_if_abandoned", "read_park_comment_marker", f"{_PKG}.park"),
    ("_armed_running_task", "find_running_task_for_session", f"{_PKG}.park"),
    ("_armed_running_task", "park_gate_open", f"{_PKG}.park"),
    (
        "_maybe_clear_staged_emit_result",
        "_write_cw_context_locked",
        f"{_PKG}.staged_emit",
    ),
    ("_resolve_and_complete_headless_session", "_apply_sentinel_to_task", _PKG),
    ("_resolve_and_complete_headless_session", "_write_cw_context_locked", _PKG),
    ("_resolve_stop_under_lock", "load_state", _PKG),
    ("_resolve_stop_under_lock", "sessions_lock", _PKG),
    ("signal_stop", "get_native_daemon_client", _PKG),
    ("signal_stop", "_write_cw_context_locked", _PKG),
    ("_handle_unrouted_stop", "get_native_daemon_client", _PKG),
]


def _function_globals(function: str) -> dict[str, object]:
    """The globals *function* reads, unwrapping the ``signal_stop`` command."""
    value = getattr(stop_hook, function)
    if isinstance(value, click.Command):
        assert value.callback is not None
        value = value.callback
    unwrapped: Callable[..., object] = inspect.unwrap(value)
    return unwrapped.__globals__


class TestPatchOwnership:
    """Guards that each patched global lives where its reader looks it up."""

    @pytest.mark.parametrize(("function", "global_name", "owner"), PATCH_OWNERSHIP)
    def test_function_reads_global_from_owner(
        self, function: str, global_name: str, owner: str
    ) -> None:
        namespace = vars(importlib.import_module(owner))
        assert _function_globals(function) is namespace
        assert global_name in namespace

    def test_package_binds_no_patched_global(self) -> None:
        """A stale ``cw.cli.stop_hook.<global>`` target fails loudly.

        The package re-exports only its own 35 names, never a third-party
        global a submodule imports, so a patch left on the package raises
        instead of resolving and silently not intercepting. A global with any
        reader still in the package itself is exempt until that reader moves.
        """
        moved = {g for _fn, g, owner in PATCH_OWNERSHIP if owner != _PKG}
        still_read_here = {g for _fn, g, owner in PATCH_OWNERSHIP if owner == _PKG}
        third_party = moved - still_read_here - EXPECTED_EXPORTS
        assert third_party
        assert sorted(g for g in third_party if hasattr(stop_hook, g)) == []


# Every record the package emits must carry the pre-split logger name
# ``cw.cli.stop_hook`` verbatim. ``caplog.at_level(..., logger=...)`` alone
# cannot catch a rename -- level inheritance and propagation make a
# ``__name__``-derived child logger (``cw.cli.stop_hook.sentinel``) pass the
# same assertions -- so these tests pin ``record.name`` exactly, filtered to the
# record the function under test emits. One case per pre-split ``logger.`` call
# site (six).
PINNED_LOGGER_NAME = "cw.cli.stop_hook"


def _names_of(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    return [r.name for r in caplog.records if needle in r.getMessage()]


def _sentinel() -> AutoDevResult:
    return AutoDevResult.model_validate(_stage_complete_payload())


class TestLoggerNamePinned:
    """Guards that the package split did not rename the emitted logger (#2496)."""

    def test_reconstruct_emitted_sentinel_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = _make_daemon_session(
            id="sess-pin-reconstruct", last_result={"status": "not-a-real-status"}
        )

        with caplog.at_level(logging.DEBUG, logger=PINNED_LOGGER_NAME):
            assert stop_hook._reconstruct_emitted_sentinel(session) is None

        assert _names_of(caplog, "failed sentinel validation") == [PINNED_LOGGER_NAME]

    def test_harvest_oserror_warning(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _raise(*_args: object, **_kwargs: object) -> None:
            message = "state file unwritable"
            raise OSError(message)

        monkeypatch.setattr("cw.cli.stop_hook.sentinel.emit_result_locked", _raise)

        with caplog.at_level(logging.DEBUG, logger=PINNED_LOGGER_NAME):
            stop_hook._harvest_last_result_through_door("sess-pin-oserror", _sentinel())

        assert _names_of(caplog, "state read/write failed") == [PINNED_LOGGER_NAME]

    def test_harvest_session_not_found_warning(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The real door raises ``EmitSessionNotFoundError`` for a missing id."""
        with caplog.at_level(logging.DEBUG, logger=PINNED_LOGGER_NAME):
            stop_hook._harvest_last_result_through_door("no-such-session", _sentinel())

        assert _names_of(caplog, "rejected by door") == [PINNED_LOGGER_NAME]

    def test_clear_agent_spawn_stamp_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        context: dict[str, object] = {
            AGENT_SPAWN_STAMP_KEY: {AGENT_SPAWN_UNRESOLVED_COUNT_KEY: 2}
        }

        with caplog.at_level(logging.DEBUG, logger=PINNED_LOGGER_NAME):
            stop_hook._clear_agent_spawn_stamp(context)

        assert _names_of(caplog, "agent_spawn_stamp cleared") == [PINNED_LOGGER_NAME]

    def test_handle_unrouted_stop_landed_terminal_info(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_native_daemon: FakeNativeDaemonClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A ``landed_terminal`` bail logs and stops the leaked DAEMON worker."""
        session = _make_daemon_session(id="sess-pin-landed", surface_ref="pinref02")
        resolution = stop_hook._HeadlessResolution(rescued=None, landed_terminal=True)
        monkeypatch.setattr(
            "cw.cli.stop_hook.get_native_daemon_client", lambda: mock_native_daemon
        )

        with caplog.at_level(logging.DEBUG, logger=PINNED_LOGGER_NAME):
            stop_hook._handle_unrouted_stop(session, {}, resolution, 3)

        assert mock_native_daemon.stop_calls == ["pinref02"]
        assert _names_of(caplog, "landed_terminal daemon stop") == [PINNED_LOGGER_NAME]

    def test_page_sentinel_unroutable_warning(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = _make_daemon_session(id="sess-pin-unroutable")

        with caplog.at_level(logging.DEBUG, logger=PINNED_LOGGER_NAME):
            stop_hook._page_sentinel_unroutable(session, {"ticket_id": "T-pin"})

        assert _names_of(caplog, "sentinel_unroutable:") == [PINNED_LOGGER_NAME]

    def test_package_logger_uses_pinned_name(self) -> None:
        assert stop_hook.logger.name == PINNED_LOGGER_NAME


# Submodules that log. Each binds its own ``logger`` to the pinned name via
# ``_constants._LOGGER_NAME``, never ``__name__``.
LOGGING_SUBMODULES = ["agent_stamp", "sentinel"]


class TestLoggerObjectsPinned:
    """Every ``logger`` in the package is the one pinned-name Logger."""

    def test_pinned_constant_and_package_logger(self) -> None:
        constants = importlib.import_module(f"{_PKG}._constants")
        assert vars(constants)["_LOGGER_NAME"] == PINNED_LOGGER_NAME
        assert stop_hook.logger is logging.getLogger(PINNED_LOGGER_NAME)

    @pytest.mark.parametrize("submodule", LOGGING_SUBMODULES)
    def test_submodule_logger_is_the_package_logger(self, submodule: str) -> None:
        module_logger = vars(importlib.import_module(f"{_PKG}.{submodule}"))["logger"]
        assert module_logger is stop_hook.logger


class TestMovedCodeCharacterization:
    """Tests-only characterization of moved lines no other test reaches."""

    def test_restore_staged_route_outcome_without_dict_result(self) -> None:
        """A non-dict ``last_result`` restores the init-False defaults."""
        session = _make_daemon_session(id="sess-restore-none", last_result=None)
        assert stop_hook._restore_staged_route_outcome(session) == (False, False)
