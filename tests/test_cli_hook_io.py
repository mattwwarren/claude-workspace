"""Tests for ``cw.cli._hook_io``'s shared hook read/write primitives (#1946).

``_hook_io`` now backs three independent hook commands (``cw agent-spawn-pre``,
``cw signal-stop``, and ``cw guard-busy-wait``) but had no dedicated test file
of its own -- every assertion about ``_context_lock`` /
``_write_cw_context_locked`` was reached indirectly through one consumer's CLI
surface. This file exercises the primitives directly so a change to the shared
discipline fails here first, rather than in whichever consumer happens to
cover the affected branch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest

from cw.cli import _hook_io
from cw.cli._hook_io import (
    GuardToggle,
    _context_lock,
    _extract_bash_command,
    _read_cw_context,
    _write_cw_context_locked,
    active_headless_context,
    enforce,
    find_cw_context,
    find_lane_config,
    resolve_guard_enabled,
)
from cw.models import HOOK_CONTEXT_RELATIVE_PATH, LaneConfig, OrchestratorConfig
from tests.conftest import (
    _headless_worktree,
    _hold_context_lock,
    _write_clients_yaml,
    _write_global_toggle,
    _write_hook_context_file,
)
from tests.test_cli_guard_busy_wait import _BASH_PRE_PAYLOAD
from tests.test_cli_subagent_policy import _spawn_payload


def _seeded_worktree(tmp_path: Path, name: str = "wt") -> Path:
    worktree = tmp_path / name
    worktree.mkdir()
    _write_hook_context_file(worktree)
    return worktree


def test_context_lock_acquires_when_uncontended(tmp_path: Path) -> None:
    """An uncontended lock yields True and releases on exit."""
    worktree = _seeded_worktree(tmp_path)
    context_path = worktree / HOOK_CONTEXT_RELATIVE_PATH

    with _context_lock(context_path) as acquired:
        assert acquired is True

    # Releasing must leave the lock immediately re-acquirable.
    with _context_lock(context_path) as acquired_again:
        assert acquired_again is True


def test_context_lock_yields_false_when_budget_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held lock exhausts the bounded retry budget and yields False.

    Never raises: the caller's fail-open path must be an ordinary branch,
    not exception handling -- both consumers run synchronously inside the
    live worker's own turn, where blocking would hang the worker itself.
    """
    monkeypatch.setattr(
        "cw.cli._hook_io._LOCK_TIMEOUT_SECS_DEFAULT", 0.05, raising=True
    )
    worktree = _seeded_worktree(tmp_path)
    context_path = worktree / HOOK_CONTEXT_RELATIVE_PATH

    with _hold_context_lock(worktree), _context_lock(context_path) as acquired:
        assert acquired is False


def test_write_cw_context_locked_applies_mutation(tmp_path: Path) -> None:
    """The happy path returns True and persists the mutated dict."""
    worktree = _seeded_worktree(tmp_path)

    def _mutate(context: dict[str, object]) -> dict[str, object]:
        context["marker_1946"] = "written"
        return context

    assert _write_cw_context_locked(str(worktree), _mutate) is True

    context = _read_cw_context(str(worktree))
    assert context is not None
    assert context["marker_1946"] == "written"


def test_write_cw_context_locked_returns_false_on_missing_file(
    tmp_path: Path,
) -> None:
    """No cw-context.json under the cwd -> silent False, no file created."""
    bare = tmp_path / "bare"
    bare.mkdir()

    calls: list[object] = []

    def _mutate(context: dict[str, object]) -> dict[str, object]:
        calls.append(context)
        return context

    assert _write_cw_context_locked(str(bare), _mutate) is False
    assert calls == []
    assert not (bare / HOOK_CONTEXT_RELATIVE_PATH).exists()


def test_write_cw_context_locked_returns_false_on_lock_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contended lock skips the write entirely -- mutate_fn never runs."""
    monkeypatch.setattr(
        "cw.cli._hook_io._LOCK_TIMEOUT_SECS_DEFAULT", 0.05, raising=True
    )
    worktree = _seeded_worktree(tmp_path)
    before = (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")

    calls: list[object] = []

    def _mutate(context: dict[str, object]) -> dict[str, object]:
        calls.append(context)
        return context

    with _hold_context_lock(worktree):
        assert _write_cw_context_locked(str(worktree), _mutate) is False

    assert calls == []
    assert (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8") == before


def test_write_cw_context_locked_returns_false_on_malformed_context(
    tmp_path: Path,
) -> None:
    """Unparseable JSON -> silent False rather than a crash in the hook."""
    worktree = tmp_path / "broken"
    (worktree / ".claude").mkdir(parents=True)
    (worktree / HOOK_CONTEXT_RELATIVE_PATH).write_text("{ not json", encoding="utf-8")

    assert _write_cw_context_locked(str(worktree), lambda ctx: ctx) is False


def test_write_cw_context_locked_swallows_mutate_fn_errors(tmp_path: Path) -> None:
    """A mutate_fn that raises fails open -- the hook must never crash."""
    worktree = _seeded_worktree(tmp_path)
    before = (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")

    def _boom(_context: dict[str, object]) -> dict[str, object]:
        msg = "mutation blew up"
        raise RuntimeError(msg)

    assert _write_cw_context_locked(str(worktree), _boom) is False
    assert (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8") == before


def test_read_cw_context_returns_none_for_non_object_payload(tmp_path: Path) -> None:
    """A context file parsing to a list (not an object) reads as None."""
    worktree = tmp_path / "listctx"
    (worktree / ".claude").mkdir(parents=True)
    (worktree / HOOK_CONTEXT_RELATIVE_PATH).write_text("[]", encoding="utf-8")

    assert _read_cw_context(str(worktree)) is None


def test_context_path_has_exactly_one_definition_in_this_module() -> None:
    """#2210: every read path joins ``HOOK_CONTEXT_RELATIVE_PATH``, not a
    second spelling of it.

    ``find_cw_context`` (#2210) and ``_read_cw_context`` each re-spelled the
    ``.claude/cw-context.json`` join inline while ``_write_cw_context_locked``
    in the same module used the shared constant — which is exactly how #2226's
    guard and this discovery would drift apart later. A literal is the
    mutation this test catches, so it asserts on the source rather than on
    behaviour (behaviour is identical either way, which is the problem).
    """
    source = Path(_hook_io.__file__).read_text(encoding="utf-8")
    assert '"cw-context.json"' not in source
    assert source.count("HOOK_CONTEXT_RELATIVE_PATH") >= 3


def test_find_cw_context_walks_up_to_the_nearest_context(tmp_path: Path) -> None:
    """The operator-run CLI guard is not handed a worktree root (#2210)."""
    worktree = _seeded_worktree(tmp_path)
    nested = worktree / "src" / "cw"
    nested.mkdir(parents=True)

    found = find_cw_context(nested)
    assert found is not None
    assert found == _read_cw_context(str(worktree))
    assert find_cw_context(tmp_path / "elsewhere") is None


def test_read_cw_context_round_trips_the_production_writer(tmp_path: Path) -> None:
    """The reader parses exactly what ``_write_hook_context`` produces."""
    worktree = _seeded_worktree(tmp_path)

    context = _read_cw_context(str(worktree))
    assert context is not None
    on_disk = json.loads(
        (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert context == on_disk


class TestActiveHeadlessContext:
    """The policy applies to headless dispatch workers and nowhere else."""

    def test_no_context_anywhere_yields_none(self, tmp_path: Path) -> None:
        """A cwd with no ancestor cw-context.json is not a dispatch worker."""
        bare = tmp_path / "bare"
        bare.mkdir()

        assert active_headless_context(_spawn_payload(bare)) is None

    def test_interactive_context_yields_none(self, tmp_path: Path) -> None:
        """``headless: false`` is an operator's own session — never guarded."""
        worktree = tmp_path / "interactive"
        worktree.mkdir()
        _write_hook_context_file(worktree, headless=False)

        assert active_headless_context(_spawn_payload(worktree)) is None

    def test_headless_context_is_returned(self, tmp_path: Path) -> None:
        worktree = _headless_worktree(tmp_path)

        context = active_headless_context(_spawn_payload(worktree))

        assert context is not None
        assert context["headless"] is True

    def test_subdirectory_cwd_still_resolves(self, tmp_path: Path) -> None:
        """A subagent that ``cd``s inside the worktree is still covered.

        ``find_cw_context`` walks upward, so the policy does not evaporate the
        moment a worker's cwd moves into ``src/``.
        """
        worktree = _headless_worktree(tmp_path)
        nested = worktree / "src" / "cw"
        nested.mkdir(parents=True)

        assert active_headless_context(_spawn_payload(nested)) is not None

    def test_missing_cwd_yields_none(self, tmp_path: Path) -> None:
        payload = {k: v for k, v in _spawn_payload(tmp_path).items() if k != "cwd"}

        assert active_headless_context(payload) is None


class TestEnforce:
    """Turning a classification into the PreToolUse exit-code contract."""

    def test_a_reason_exits_2_with_it_on_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            enforce("nope")

        assert excinfo.value.code == 2
        assert "nope" in capsys.readouterr().err

    def test_none_verdict_is_a_pure_noop(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        enforce(None)

        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""


class TestExtractBashCommand:
    """The shared Bash-payload reader, warning through a caller-owned callback."""

    def test_extracts_command_and_background_flag(self) -> None:
        warnings: list[str] = []
        payload = {
            **_BASH_PRE_PAYLOAD,
            "tool_input": {"command": "uv run pytest", "run_in_background": True},
        }

        assert _extract_bash_command(payload, warnings.append) == (
            "uv run pytest",
            True,
        )
        assert warnings == []

    def test_malformed_tool_input_calls_warn_and_allows(self) -> None:
        warnings: list[str] = []
        payload = {**_BASH_PRE_PAYLOAD, "tool_input": "not-a-dict"}

        assert _extract_bash_command(payload, warnings.append) == (None, False)
        assert len(warnings) == 1
        assert "tool_input is str, expected dict" in warnings[0]

    def test_non_bool_run_in_background_calls_warn_and_defaults_false(self) -> None:
        warnings: list[str] = []
        payload = {
            **_BASH_PRE_PAYLOAD,
            "tool_input": {"command": "uv run pytest", "run_in_background": "yes"},
        }

        assert _extract_bash_command(payload, warnings.append) == (
            "uv run pytest",
            False,
        )
        assert len(warnings) == 1
        assert "run_in_background is str, expected bool" in warnings[0]


_GUARD_TOGGLES = get_args(GuardToggle)


class TestGuardToggleFields:
    def test_toggle_set_covers_both_default_on_guards(self) -> None:
        assert set(_GUARD_TOGGLES) == {
            "subagent_spawn_guard_enabled",
            "background_tool_guard_enabled",
        }

    @pytest.mark.parametrize("toggle", _GUARD_TOGGLES)
    def test_every_toggle_is_a_field_on_both_config_layers(self, toggle: str) -> None:
        """The resolver reads the toggle by name off both models; a rename of
        either field must fail here rather than as an AttributeError in a hook
        that fails open and so would never surface it."""
        assert toggle in OrchestratorConfig.model_fields
        assert toggle in LaneConfig.model_fields


@pytest.mark.parametrize("toggle", _GUARD_TOGGLES)
class TestResolveGuardEnabled:
    """Lane-then-global fallthrough shared by every default-on guard toggle.

    Relocated from ``TestResolveSpawnGuardEnabled`` (#2211) and
    ``TestResolveBackgroundToolGuardEnabled`` (#2303) once both guards'
    resolvers collapsed into :func:`cw.cli._hook_io.resolve_guard_enabled`:
    the same six cases, now run against each toggle.
    """

    def test_defaults_on_with_no_client_or_lane(self, toggle: GuardToggle) -> None:
        assert resolve_guard_enabled(None, None, toggle) is True

    def test_global_disable_wins_with_no_lane_override(
        self, toggle: GuardToggle, tmp_config_dir: Path
    ) -> None:
        _write_global_toggle(tmp_config_dir, toggle, "false")

        assert resolve_guard_enabled(None, None, toggle) is False

    def test_lane_override_disables_against_enabled_global(
        self, toggle: GuardToggle, tmp_config_dir: Path
    ) -> None:
        _write_clients_yaml(tmp_config_dir, lane_value="false", field_name=toggle)

        assert resolve_guard_enabled("acme", "fast", toggle) is False

    def test_lane_override_enables_against_disabled_global(
        self, toggle: GuardToggle, tmp_config_dir: Path
    ) -> None:
        """The override is bidirectional — a lane can turn the guard back ON."""
        _write_global_toggle(tmp_config_dir, toggle, "false")
        _write_clients_yaml(tmp_config_dir, lane_value="true", field_name=toggle)

        assert resolve_guard_enabled("acme", "fast", toggle) is True

    def test_unknown_client_falls_through_to_global(
        self, toggle: GuardToggle, tmp_config_dir: Path
    ) -> None:
        _write_clients_yaml(tmp_config_dir, lane_value="false", field_name=toggle)

        assert resolve_guard_enabled("not-a-client", "fast", toggle) is True

    def test_unknown_lane_falls_through_to_global(
        self, toggle: GuardToggle, tmp_config_dir: Path
    ) -> None:
        _write_clients_yaml(tmp_config_dir, lane_value="false", field_name=toggle)

        assert resolve_guard_enabled("acme", "not-a-lane", toggle) is True

    def test_other_toggles_lane_override_is_ignored(
        self, toggle: GuardToggle, tmp_config_dir: Path
    ) -> None:
        """Each guard reads its own field, never a sibling guard's."""
        other = next(name for name in _GUARD_TOGGLES if name != toggle)
        _write_clients_yaml(tmp_config_dir, lane_value="false", field_name=other)

        assert resolve_guard_enabled("acme", "fast", toggle) is True


class TestFindLaneConfig:
    """The declared-lane lookup every lane-overridable hook guard shares."""

    def test_returns_the_declared_lane(self, tmp_config_dir: Path) -> None:
        _write_clients_yaml(tmp_config_dir, lane_value="false")

        lane_cfg = find_lane_config("acme", "fast")

        assert lane_cfg is not None
        assert lane_cfg.name == "fast"
        assert lane_cfg.subagent_spawn_guard_enabled is False

    @pytest.mark.parametrize(
        ("client", "lane"),
        [(None, "fast"), ("acme", None), ("", "fast"), ("acme", "")],
    )
    def test_missing_client_or_lane_yields_none(
        self, tmp_config_dir: Path, client: str | None, lane: str | None
    ) -> None:
        _write_clients_yaml(tmp_config_dir, lane_value="false")

        assert find_lane_config(client, lane) is None

    def test_unknown_client_yields_none(self, tmp_config_dir: Path) -> None:
        _write_clients_yaml(tmp_config_dir, lane_value="false")

        assert find_lane_config("not-a-client", "fast") is None

    def test_undeclared_lane_yields_none(self, tmp_config_dir: Path) -> None:
        _write_clients_yaml(tmp_config_dir, lane_value="false")

        assert find_lane_config("acme", "not-a-lane") is None
