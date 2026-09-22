"""Tests for the ``cw agent-spawn-pre`` spawn-shape policy (#2211).

#2017 established that cw must own the launch of any agent it is responsible
for monitoring: a harness subagent never enters the session roster, so cw can
neither see it start nor stop it. #2211 observed the uncovered path — an impl
worker forked a subagent for a read-only lookup, the fork inherited the
implementation mandate, and it committed and pushed before the parent's
stop message won the race.

:mod:`cw.cli._subagent_policy` is the enforcement surface. In a headless
dispatch worker it refuses an explicit ``fork``/blank ``subagent_type`` and,
on the same terms, a spawn that names no type at all. Denying that second case
waited on a complete spawn-site inventory (residual gap R7) — a refusal a
caller cannot correctly retry is an outage, not a guard — and shipped once
``review-sweep.md``'s six roles resolved to ``general-purpose``. Everything
else fails open.

Payload fixtures here are derived from the **real** captured ``PreToolUse``
payload in ``tests/test_cli_agent_spawn_stamp.py`` by mutating only
``tool_input.subagent_type`` — the one field under test. The base capture's
shape (including the keys the handler ignores) is preserved rather than
hand-authored.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.cli._subagent_policy import (
    _resolve_spawn_guard_enabled,
    active_headless_context,
    classify_spawn,
    enforce,
)
from tests.conftest import _headless_worktree, _write_hook_context_file
from tests.test_cli_agent_spawn_stamp import _PRE_PAYLOAD, _pre_tool_input

if TYPE_CHECKING:
    from pathlib import Path

_ABSENT = object()


def _spawn_payload(cwd: Path, subagent_type: object = _ABSENT) -> dict[str, object]:
    """Return the real capture rebased on *cwd*, with *subagent_type* applied.

    ``_ABSENT`` deletes the key entirely — the "spawned with no type named at
    all" shape, which is distinct from a present-but-blank value and is the
    only one of the two that this policy allows.
    """
    tool_input = _pre_tool_input()
    if subagent_type is _ABSENT:
        del tool_input["subagent_type"]
    else:
        tool_input["subagent_type"] = subagent_type
    return {**_PRE_PAYLOAD, "cwd": str(cwd), "tool_input": tool_input}


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


class TestResolveSpawnGuardEnabled:
    """Lane-then-global fallthrough, mirroring the #1946 busy-wait precedent."""

    def test_defaults_on_with_no_client_or_lane(self) -> None:
        assert _resolve_spawn_guard_enabled(None, None) is True

    def test_global_disable_wins_with_no_lane_override(
        self, tmp_config_dir: Path
    ) -> None:
        orchestrator_path = tmp_config_dir / ".claude-workspace" / "orchestrator.yaml"
        orchestrator_path.parent.mkdir(parents=True, exist_ok=True)
        orchestrator_path.write_text("subagent_spawn_guard_enabled: false\n")

        assert _resolve_spawn_guard_enabled(None, None) is False

    def test_lane_override_disables_against_enabled_global(
        self, tmp_config_dir: Path
    ) -> None:
        _write_clients_yaml(tmp_config_dir, lane_value="false")

        assert _resolve_spawn_guard_enabled("acme", "fast") is False

    def test_lane_override_enables_against_disabled_global(
        self, tmp_config_dir: Path
    ) -> None:
        """The override is bidirectional — a lane can turn the guard back ON."""
        orchestrator_path = tmp_config_dir / ".claude-workspace" / "orchestrator.yaml"
        orchestrator_path.parent.mkdir(parents=True, exist_ok=True)
        orchestrator_path.write_text("subagent_spawn_guard_enabled: false\n")
        _write_clients_yaml(tmp_config_dir, lane_value="true")

        assert _resolve_spawn_guard_enabled("acme", "fast") is True

    def test_unknown_client_falls_through_to_global(self, tmp_config_dir: Path) -> None:
        _write_clients_yaml(tmp_config_dir, lane_value="false")

        assert _resolve_spawn_guard_enabled("not-a-client", "fast") is True

    def test_unknown_lane_falls_through_to_global(self, tmp_config_dir: Path) -> None:
        _write_clients_yaml(tmp_config_dir, lane_value="false")

        assert _resolve_spawn_guard_enabled("acme", "not-a-lane") is True


def _write_clients_yaml(tmp_config_dir: Path, lane_value: str) -> None:
    """Write a one-client, one-lane clients.yaml carrying the lane override."""
    ws_dir = tmp_config_dir / "ws"
    ws_dir.mkdir(exist_ok=True)
    clients_path = tmp_config_dir / ".config" / "cw" / "clients.yaml"
    clients_path.parent.mkdir(parents=True, exist_ok=True)
    clients_path.write_text(
        "clients:\n"
        "  acme:\n"
        f"    workspace_path: {ws_dir}\n"
        "    lanes:\n"
        "      - name: fast\n"
        f"        subagent_spawn_guard_enabled: {lane_value}\n"
    )


class TestClassifySpawn:
    """The decision table, in a headless, guard-enabled worker."""

    @pytest.mark.parametrize("subagent_type", ["fork", "FORK", "Fork", "fOrK"])
    def test_explicit_fork_is_denied_case_insensitively(
        self, tmp_path: Path, subagent_type: str
    ) -> None:
        worktree = _headless_worktree(tmp_path)

        reason = classify_spawn(_spawn_payload(worktree, subagent_type))

        assert reason is not None
        assert "#2211" in reason

    def test_blank_subagent_type_is_denied(self, tmp_path: Path) -> None:
        """A present-but-blank value is a fork by another name, not an omission."""
        worktree = _headless_worktree(tmp_path)

        assert classify_spawn(_spawn_payload(worktree, "")) is not None

    def test_omitted_subagent_type_is_denied(self, tmp_path: Path) -> None:
        """Refused since the spawn-site inventory closed (was record-only, R7)."""
        worktree = _headless_worktree(tmp_path)

        reason = classify_spawn(_spawn_payload(worktree))

        assert reason is not None
        assert "#2211" in reason

    def test_omitted_type_refusal_names_what_to_retry_with(
        self, tmp_path: Path
    ) -> None:
        """The reason is the only channel the refused caller has.

        Denial is only defensible because a correct retry exists — the
        inventory made sure of that — so the message has to say what it is.
        """
        worktree = _headless_worktree(tmp_path)

        reason = classify_spawn(_spawn_payload(worktree))

        assert reason is not None
        assert "general-purpose" in reason
        assert "Read Only Helper" in reason
        assert "subagent_spawn_guard_enabled" in reason

    def test_null_subagent_type_is_denied(self, tmp_path: Path) -> None:
        """JSON ``null`` reads as "no type named", not as a blank string."""
        worktree = _headless_worktree(tmp_path)

        assert classify_spawn(_spawn_payload(worktree, None)) is not None

    @pytest.mark.parametrize(
        "subagent_type", ["general-purpose", "Explore", "Read Only Helper"]
    )
    def test_named_type_allows_silently(
        self, tmp_path: Path, subagent_type: str
    ) -> None:
        worktree = _headless_worktree(tmp_path)

        assert classify_spawn(_spawn_payload(worktree, subagent_type)) is None

    def test_non_agent_tool_name_yields_no_verdict(self, tmp_path: Path) -> None:
        """Belt-and-braces: the matcher already scopes this to Agent/Task."""
        worktree = _headless_worktree(tmp_path)
        payload = {**_spawn_payload(worktree, "fork"), "tool_name": "Bash"}

        assert classify_spawn(payload) is None

    def test_task_tool_name_is_covered(self, tmp_path: Path) -> None:
        worktree = _headless_worktree(tmp_path)
        payload = {**_spawn_payload(worktree, "fork"), "tool_name": "Task"}

        assert classify_spawn(payload) is not None

    def test_malformed_tool_input_warns_and_allows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unexpected payload shape fails open, loudly (#1946's idiom)."""
        worktree = _headless_worktree(tmp_path)
        payload = {**_spawn_payload(worktree, "fork"), "tool_input": "not-a-dict"}

        assert classify_spawn(payload) is None
        assert "#2211" in capsys.readouterr().err

    def test_non_string_subagent_type_warns_and_allows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        worktree = _headless_worktree(tmp_path)

        assert classify_spawn(_spawn_payload(worktree, 7)) is None
        assert "#2211" in capsys.readouterr().err

    def test_none_payload_yields_no_verdict(self) -> None:
        """Unreadable stdin upstream must not become a block."""
        assert classify_spawn(None) is None

    def test_disabled_guard_allows_an_explicit_fork(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The config gate is a real kill switch, not just a softener."""
        monkeypatch.setattr(
            "cw.cli._subagent_policy._resolve_spawn_guard_enabled",
            lambda _client, _lane: False,
        )
        worktree = _headless_worktree(tmp_path)

        assert classify_spawn(_spawn_payload(worktree, "fork")) is None

    def test_interactive_worker_allows_an_explicit_fork(self, tmp_path: Path) -> None:
        """An operator's deliberate interactive fork is never refused."""
        worktree = tmp_path / "interactive"
        worktree.mkdir()
        _write_hook_context_file(worktree, headless=False)

        assert classify_spawn(_spawn_payload(worktree, "fork")) is None

    def test_no_context_allows_an_explicit_fork(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare"
        bare.mkdir()

        assert classify_spawn(_spawn_payload(bare, "fork")) is None


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
