"""Tests for the ``cw guard-busy-wait`` PreToolUse hook command (#1946).

The guard blocks a Bash tool call (exit 2) when the worker is busy-waiting:
a bare ``true``/``:`` no-op, a bare ``sleep``, or the same command repeated
past a configured threshold inside a rolling window. Every other outcome --
a substantive command, a backgrounded call, a missing/malformed context, an
unreadable stdin, a disabled guard -- is a best-effort no-op (exit 0), so a
broken hook never wedges every Bash call in every worker.

**Fixture provenance (#1946 R1).** ``_BASH_PRE_PAYLOAD`` below is NOT a
capture. Its *envelope* keys (``session_id``, ``transcript_path``, ``cwd``,
``prompt_id``, ``permission_mode``, ``hook_event_name``, ``tool_use_id``)
are copied from the real captured ``PreToolUse`` payload in
``tests/test_cli_agent_spawn_stamp.py`` (``_PRE_PAYLOAD``, captured
2026-08-12 against Claude Code via a temporary catch-all hook in a live
dispatch worktree). That capture also establishes that ``tool_input`` is a
flat dict of the tool's own parameter names and that ``run_in_background``
is a genuine key inside it for at least one tool. The Bash-specific
``tool_input`` field names (``command``, ``run_in_background``) are
**inferred, not captured** -- no Bash-tool PreToolUse payload exists in this
repo. The production handler therefore reads every field defensively and
emits a loud one-line warning when the shape does not match, so a wrong
inference degrades to "guard does not fire, and says so on every call"
rather than "guard silently never fires."
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner
from freezegun import freeze_time

from cw.cli import main
from cw.cli.guard_busy_wait import (
    _BUSY_WAIT_COMMAND_HASH_KEY,
    _BUSY_WAIT_HISTORY_MAXLEN,
    _BUSY_WAIT_RECENT_COMMANDS_KEY,
    _BUSY_WAIT_STATE_KEY,
    _hash_command,
)
from cw.config import clients_file, orchestrator_config_file
from cw.events import read_events
from cw.models import HOOK_CONTEXT_RELATIVE_PATH, OrchestratorEventType
from tests.conftest import (
    _hold_context_lock,
    _invoke_hook_command,
    _write_hook_context_file,
)

if TYPE_CHECKING:
    from pathlib import Path

_BLOCK_EXIT = 2

# See the module docstring for the captured-vs-inferred split. Only ``cwd``
# and the two ``tool_input`` fields are varied per-test; every other key is
# preserved from the captured envelope so the fixture keeps the shape
# production actually delivers.
_BASH_PRE_PAYLOAD: dict[str, object] = {
    "session_id": "00000000-0000-0000-0000-000000000000",
    "transcript_path": "/home/redacted/.claude/projects/-redacted/redacted.jsonl",
    "cwd": "/redacted",
    "prompt_id": "00000000-0000-0000-0000-000000000001",
    "permission_mode": "auto",
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {
        "command": "true",
        "run_in_background": False,
    },
    "tool_use_id": "toolu_0000000000000000000000",
}


def _payload(
    cwd: Path, command: object = "true", *, run_in_background: bool = False
) -> dict[str, object]:
    """Return the Bash payload pointed at *cwd* carrying *command*."""
    return {
        **_BASH_PRE_PAYLOAD,
        "cwd": str(cwd),
        "tool_input": {"command": command, "run_in_background": run_in_background},
    }


def _worktree(tmp_path: Path, name: str = "wt") -> Path:
    """Create a worktree carrying a freshly-written cw-context.json."""
    worktree = tmp_path / name
    worktree.mkdir()
    _write_hook_context_file(worktree)
    return worktree


def _invoke(cwd: Path, command: object = "true", **kwargs: Any) -> Any:
    return _invoke_hook_command("guard-busy-wait", _payload(cwd, command, **kwargs))


def _state(worktree: Path) -> dict[str, Any]:
    """Return the guard's on-disk state block (empty dict when absent)."""
    context = json.loads(
        (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    block = context.get(_BUSY_WAIT_STATE_KEY, {})
    assert isinstance(block, dict)
    return block


def _recent(worktree: Path) -> list[dict[str, Any]]:
    entries = _state(worktree).get(_BUSY_WAIT_RECENT_COMMANDS_KEY, [])
    assert isinstance(entries, list)
    return entries


def _blocked_events() -> list[Any]:
    return read_events(
        event_types=[OrchestratorEventType.GUARD_BUSY_WAIT_BLOCKED],
    )


def _write_lane_client(tmp_path: Path, lane_block: str) -> None:
    """Write a clients.yaml declaring ``client-a`` with one configured lane."""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    clients_file().write_text(
        "clients:\n"
        "  client-a:\n"
        f"    workspace_path: {workspace}\n"
        "    lanes:\n"
        "      - name: fast\n" + lane_block,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Core classification
# ---------------------------------------------------------------------------


def test_blocks_bare_true(tmp_path: Path) -> None:
    """A bare ``true`` no-op is rejected on the first call."""
    worktree = _worktree(tmp_path)
    result = _invoke(worktree, "true")

    assert result.exit_code == _BLOCK_EXIT
    assert "#1946" in result.output
    assert "busy-wait" in result.output
    assert f"command_hash={_hash_command('true')}" in result.output


def test_blocks_bare_colon_noop(tmp_path: Path) -> None:
    """A bare ``:`` no-op is rejected."""
    worktree = _worktree(tmp_path)
    assert _invoke(worktree, ":").exit_code == _BLOCK_EXIT


def test_blocks_bare_sleep(tmp_path: Path) -> None:
    """A bare ``sleep N`` with no follow-on work is rejected."""
    worktree = _worktree(tmp_path)
    assert _invoke(worktree, "sleep 30").exit_code == _BLOCK_EXIT


def test_allows_sleep_inside_substantive_script(tmp_path: Path) -> None:
    """``sleep`` chained to real work is not a busy-wait -- allowed."""
    worktree = _worktree(tmp_path)
    assert _invoke(worktree, "sleep 5 && ./run_tests.sh").exit_code == 0


def test_allows_run_in_background_true_even_for_noop_command(tmp_path: Path) -> None:
    """A backgrounded call never holds the turn open -- exempt."""
    worktree = _worktree(tmp_path)
    assert _invoke(worktree, "true", run_in_background=True).exit_code == 0


def test_blocks_identical_command_repeated_n_times_in_window(tmp_path: Path) -> None:
    """The default threshold (3) blocks the third identical call in-window."""
    worktree = _worktree(tmp_path)

    assert _invoke(worktree, "git status").exit_code == 0
    assert _invoke(worktree, "git status").exit_code == 0
    assert _invoke(worktree, "git status").exit_code == _BLOCK_EXIT


def test_allows_identical_command_outside_window(tmp_path: Path) -> None:
    """Entries older than the resolved window are pruned before counting."""
    worktree = _worktree(tmp_path)

    with freeze_time("2026-08-20T12:00:00+00:00"):
        assert _invoke(worktree, "git status").exit_code == 0
        assert _invoke(worktree, "git status").exit_code == 0
    with freeze_time("2026-08-20T12:10:00+00:00"):
        assert _invoke(worktree, "git status").exit_code == 0


def test_allows_distinct_status_commands(tmp_path: Path) -> None:
    """Distinct commands never accumulate toward one another's threshold."""
    worktree = _worktree(tmp_path)

    for command in ("git status", "git log -1", "git diff --stat"):
        assert _invoke(worktree, command).exit_code == 0


def test_regression_9441b655_shape_blocks_on_first_call(tmp_path: Path) -> None:
    """The incident shape (repeated bare ``true``) trips immediately."""
    worktree = _worktree(tmp_path)

    assert _invoke(worktree, "true").exit_code == _BLOCK_EXIT
    assert _recent(worktree) == []


# ---------------------------------------------------------------------------
# Fail-open parity with cw guard-cwd
# ---------------------------------------------------------------------------


def test_noop_on_missing_tool_input(tmp_path: Path) -> None:
    """A payload with no ``tool_input`` at all is unclassifiable -- exit 0."""
    worktree = _worktree(tmp_path)
    payload = {**_BASH_PRE_PAYLOAD, "cwd": str(worktree)}
    del payload["tool_input"]

    assert _invoke_hook_command("guard-busy-wait", payload).exit_code == 0


def test_noop_on_non_bash_command_key(tmp_path: Path) -> None:
    """``tool_input`` present but carrying no ``command`` key -- exit 0."""
    worktree = _worktree(tmp_path)
    payload = {
        **_BASH_PRE_PAYLOAD,
        "cwd": str(worktree),
        "tool_input": {"description": "no command here"},
    }

    assert _invoke_hook_command("guard-busy-wait", payload).exit_code == 0


def test_noop_on_malformed_stdin() -> None:
    """Non-JSON stdin -> exit 0."""
    result = CliRunner().invoke(main, ["guard-busy-wait"], input="not json at all")
    assert result.exit_code == 0


def test_noop_on_empty_stdin() -> None:
    """Empty stdin -> exit 0."""
    result = CliRunner().invoke(main, ["guard-busy-wait"], input="")
    assert result.exit_code == 0


def test_noop_on_context_read_failure(tmp_path: Path) -> None:
    """A missing cw-context.json is not a reason to block -- exit 0.

    The bare-no-op rules do not need state, but a worktree with no context
    also has no client/lane to resolve config from, so the guard must fall
    through to the global default and still write nothing.
    """
    bare = tmp_path / "bare"
    bare.mkdir()

    assert _invoke(bare, "git status").exit_code == 0
    assert not (bare / HOOK_CONTEXT_RELATIVE_PATH).exists()


def test_noop_on_lock_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held per-worktree lock exhausts the retry budget and fails open.

    Patches ``cw.cli._hook_io._LOCK_TIMEOUT_SECS_DEFAULT`` where it is
    *defined* -- ``_context_lock`` reads that module-global, so patching a
    re-exported copy would silently be a no-op (#1947).
    """
    monkeypatch.setattr(
        "cw.cli._hook_io._LOCK_TIMEOUT_SECS_DEFAULT", 0.05, raising=True
    )
    worktree = _worktree(tmp_path)

    with _hold_context_lock(worktree):
        for _ in range(5):
            assert _invoke(worktree, "git status").exit_code == 0

    assert _recent(worktree) == []


def test_survives_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash inside classification is swallowed -> exit 0."""

    def _boom() -> object:
        msg = "unexpected"
        raise RuntimeError(msg)

    monkeypatch.setattr("cw.cli.guard_busy_wait._classify", _boom)
    result = CliRunner().invoke(
        main, ["guard-busy-wait"], input=json.dumps({"cwd": "/x"})
    )
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# R7 -- hashed state, never raw command text
# ---------------------------------------------------------------------------


def test_state_written_atomically_and_pruned(tmp_path: Path) -> None:
    """Rolling-window state is capped and stores hashes, not raw text."""
    worktree = _worktree(tmp_path)

    for index in range(_BUSY_WAIT_HISTORY_MAXLEN + 5):
        assert _invoke(worktree, f"echo distinct-{index}").exit_code == 0

    entries = _recent(worktree)
    assert len(entries) <= _BUSY_WAIT_HISTORY_MAXLEN
    assert all(_BUSY_WAIT_COMMAND_HASH_KEY in entry for entry in entries)
    assert all("command" not in entry for entry in entries)


def test_state_never_stores_raw_command_text(tmp_path: Path) -> None:
    """A secret-bearing command leaves no plaintext trace in cw-context.json."""
    worktree = _worktree(tmp_path)
    command = 'curl -H "Authorization: Bearer sekrit123" https://example.com'

    assert _invoke(worktree, command).exit_code == 0
    assert _invoke(worktree, command).exit_code == 0

    raw = (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "sekrit123" not in raw
    assert command not in raw
    assert _hash_command(command) in raw


def test_hash_command_normalizes_whitespace() -> None:
    """Whitespace-equivalent commands hash identically; distinct ones do not."""
    assert _hash_command("git status") == _hash_command("git   status")
    assert _hash_command("git status") == _hash_command("  git status  ")
    assert _hash_command("git status") != _hash_command("git log")


# ---------------------------------------------------------------------------
# R8 -- observable block record
# ---------------------------------------------------------------------------


def test_block_emits_guard_busy_wait_blocked_event(tmp_path: Path) -> None:
    """A block records exactly one durable bus event naming reason + hash."""
    worktree = _worktree(tmp_path)

    assert _invoke(worktree, "true").exit_code == _BLOCK_EXIT

    events = _blocked_events()
    assert len(events) == 1
    payload = events[0].payload
    assert payload["reason"] == "bare_noop"
    assert payload["command_hash"] == _hash_command("true")
    assert payload["client"] == "client-a"


def test_repeat_block_event_carries_threshold_and_window(tmp_path: Path) -> None:
    """The repeat-threshold reason carries the resolved numbers it tripped."""
    worktree = _worktree(tmp_path)

    for _ in range(2):
        assert _invoke(worktree, "git status").exit_code == 0
    assert _invoke(worktree, "git status").exit_code == _BLOCK_EXIT

    events = _blocked_events()
    assert len(events) == 1
    assert events[0].payload["reason"] == "repeat_threshold"
    assert events[0].payload["repeat_threshold"] == 3
    assert events[0].payload["window_seconds"] == 300


def test_allow_emits_no_event(tmp_path: Path) -> None:
    """A non-blocking call records zero guard.busy_wait_blocked events."""
    worktree = _worktree(tmp_path)

    assert _invoke(worktree, "./run_tests.sh").exit_code == 0
    assert _blocked_events() == []


def test_block_record_event_failure_does_not_suppress_the_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken event bus must never turn a detected busy-wait into an allow."""

    def _boom(*_args: object, **_kwargs: object) -> object:
        msg = "inbox unwritable"
        raise OSError(msg)

    monkeypatch.setattr("cw.cli.guard_busy_wait.record_event", _boom)
    worktree = _worktree(tmp_path)

    result = _invoke(worktree, "true")

    assert result.exit_code == _BLOCK_EXIT
    assert "#1946" in result.output


def test_block_message_names_guard_threshold_and_hash(tmp_path: Path) -> None:
    """The stderr record names the guard, the threshold, and the hash (R8)."""
    worktree = _worktree(tmp_path)

    for _ in range(2):
        assert _invoke(worktree, "git status").exit_code == 0
    result = _invoke(worktree, "git status")

    assert result.exit_code == _BLOCK_EXIT
    assert "guard-busy-wait" in result.output
    assert "threshold=3/300s" in result.output
    assert f"command_hash={_hash_command('git status')}" in result.output


# ---------------------------------------------------------------------------
# R1 -- defensive tool_input access
# ---------------------------------------------------------------------------


def test_warns_loudly_on_missing_tool_input(tmp_path: Path) -> None:
    """An absent ``tool_input`` is anomalous for a Bash matcher -- warn loudly."""
    worktree = _worktree(tmp_path)
    payload = {**_BASH_PRE_PAYLOAD, "cwd": str(worktree)}
    del payload["tool_input"]

    result = _invoke_hook_command("guard-busy-wait", payload)

    assert result.exit_code == 0
    assert "WARN" in result.output
    assert "guard-busy-wait" in result.output
    assert "#1946" in result.output
    assert "tool_input" in result.output


def test_warns_loudly_on_non_string_command(tmp_path: Path) -> None:
    """A non-string ``tool_input.command`` warns and names the observed type."""
    worktree = _worktree(tmp_path)

    result = _invoke(worktree, 123)

    assert result.exit_code == 0
    assert "WARN" in result.output
    assert "guard-busy-wait" in result.output
    assert "#1946" in result.output
    assert "int" in result.output


def test_no_warning_on_empty_stdin() -> None:
    """Unreadable stdin is the routine, silent, cross-hook fail-open case."""
    result = CliRunner().invoke(main, ["guard-busy-wait"], input="")

    assert result.exit_code == 0
    assert "WARN" not in result.output


# ---------------------------------------------------------------------------
# Config gate (R6) -- per-lane with global default
# ---------------------------------------------------------------------------


def test_disabled_globally_allows_everything(tmp_path: Path) -> None:
    """``busy_wait_guard_enabled: false`` allows even a repeated bare no-op."""
    orchestrator_config_file().parent.mkdir(parents=True, exist_ok=True)
    orchestrator_config_file().write_text(
        "busy_wait_guard_enabled: false\n", encoding="utf-8"
    )
    worktree = _worktree(tmp_path)

    for _ in range(4):
        assert _invoke(worktree, "true").exit_code == 0

    context = json.loads(
        (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert _BUSY_WAIT_STATE_KEY not in context


def test_enabled_globally_is_default(tmp_path: Path) -> None:
    """With no override the guard is on -- the unconfigured default is enabled."""
    worktree = _worktree(tmp_path)
    assert _invoke(worktree, "true").exit_code == _BLOCK_EXIT


def test_lane_override_disables_when_global_enabled(tmp_path: Path) -> None:
    """A lane opting out wins over the enabled-by-default global."""
    _write_lane_client(tmp_path, "        busy_wait_guard_enabled: false\n")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_hook_context_file(worktree, lane="fast")

    assert _invoke(worktree, "true").exit_code == 0


def test_lane_override_enables_when_global_disabled(tmp_path: Path) -> None:
    """A lane opting in wins over a disabled global -- bidirectional override."""
    orchestrator_config_file().parent.mkdir(parents=True, exist_ok=True)
    orchestrator_config_file().write_text(
        "busy_wait_guard_enabled: false\n", encoding="utf-8"
    )
    _write_lane_client(tmp_path, "        busy_wait_guard_enabled: true\n")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_hook_context_file(worktree, lane="fast")

    assert _invoke(worktree, "true").exit_code == _BLOCK_EXIT


def test_lane_overrides_repeat_threshold_and_window(tmp_path: Path) -> None:
    """A lane threshold of 2 blocks on the second occurrence, not the third."""
    _write_lane_client(
        tmp_path,
        "        busy_wait_guard_repeat_threshold: 2\n"
        "        busy_wait_guard_window_seconds: 120\n",
    )
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_hook_context_file(worktree, lane="fast")

    assert _invoke(worktree, "git status").exit_code == 0
    result = _invoke(worktree, "git status")

    assert result.exit_code == _BLOCK_EXIT
    assert "threshold=2/120s" in result.output


def test_missing_lane_key_in_context_falls_back_to_global(tmp_path: Path) -> None:
    """A pre-#1946 context (no ``lane`` key) resolves the global default."""
    _write_lane_client(tmp_path, "        busy_wait_guard_enabled: false\n")
    worktree = _worktree(tmp_path)
    context_path = worktree / HOOK_CONTEXT_RELATIVE_PATH
    context = json.loads(context_path.read_text(encoding="utf-8"))
    del context["lane"]
    context_path.write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")

    assert _invoke(worktree, "true").exit_code == _BLOCK_EXIT


def test_unknown_client_in_context_falls_back_to_global(tmp_path: Path) -> None:
    """A context naming a client absent from clients.yaml uses the global."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_hook_context_file(worktree, lane="fast")

    assert _invoke(worktree, "true").exit_code == _BLOCK_EXIT
