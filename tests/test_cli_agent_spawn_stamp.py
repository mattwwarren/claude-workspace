"""Tests for the ``cw agent-spawn-pre`` hook (#1646, split by #1947).

Originally a Pre/Post pair: ``agent-spawn-pre`` increments the "unresolved
subagent spawn" counter in the worktree's ``.claude/cw-context.json`` before
a subagent tool call starts, ``agent-spawn-post`` decremented it when that
call returned. #1947 removed the Post half — replaying a live async
``Agent(isolation="worktree")`` spawn showed ``PostToolUse:Agent`` fires at
launch-return (the ``Async agent launched successfully.`` tool_result), not
subagent completion, so the pair balanced back to 0 while the harness's own
turn accounting still reported the background agent pending. ``cw
signal-stop`` (``cli/stop_hook.py``, tested in ``tests/test_cli_stop_hook.py``)
now owns clearing/snapshotting the counter instead, driven off the hook
payload's own ``background_tasks`` list.

``agent-spawn-pre`` mirrors ``cw guard-cwd``'s fail-open contract: unreadable
stdin, a missing/malformed context, or a contended lock all yield a silent
exit 0. It never blocks a tool call at all — there is no failure mode in
which refusing a subagent spawn is the right answer.

Hook payload fixtures in this file are redacted copies of a **real** captured
``PreToolUse`` payload (captured 2026-08-12 against Claude Code via a
temporary catch-all hook in a live dispatch worktree), not hand-authored
from prose. That capture is also what settled the matcher name: the subagent
tool reports ``"tool_name": "Agent"``, not ``"Task"``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from cw.cli._hook_io import _LOCK_TIMEOUT_SECS_DEFAULT, _context_lock
from cw.cli.agent_spawn_stamp import _last_stamped_at, _unresolved_count
from cw.models import (
    AGENT_SPAWN_STAMP_KEY,
    AGENT_SPAWN_UNRESOLVED_COUNT_KEY,
    HOOK_CONTEXT_RELATIVE_PATH,
)
from tests.conftest import _invoke_hook_command, _write_hook_context_file

if TYPE_CHECKING:
    from pathlib import Path

# Redacted capture of a real PreToolUse payload for a subagent spawn. Only the
# ``cwd`` is substituted per-test; every other key (including the ones the
# handler ignores) is preserved from the capture so the fixture keeps the
# shape production actually delivers.
_PRE_PAYLOAD: dict[str, object] = {
    "session_id": "00000000-0000-0000-0000-000000000000",
    "transcript_path": "/home/redacted/.claude/projects/-redacted/redacted.jsonl",
    "cwd": "/redacted",
    "prompt_id": "00000000-0000-0000-0000-000000000001",
    "permission_mode": "auto",
    "agent_id": "0000000000000000a",
    "agent_type": "general-purpose",
    "effort": {"level": "high"},
    "hook_event_name": "PreToolUse",
    "tool_name": "Agent",
    "tool_input": {
        "description": "Trivial matcher probe",
        "prompt": "redacted",
        "subagent_type": "Explore",
        "model": "haiku",
        "run_in_background": False,
    },
    "tool_use_id": "toolu_0000000000000000000000",
}


def _payload(base: dict[str, object], cwd: Path) -> dict[str, object]:
    """Return *base* with ``cwd`` pointed at the test worktree."""
    return {**base, "cwd": str(cwd)}


def _read_count(worktree: Path) -> int:
    """Return the on-disk unresolved-spawn count for *worktree*."""
    context = json.loads(
        (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    stamp = context[AGENT_SPAWN_STAMP_KEY]
    count = stamp[AGENT_SPAWN_UNRESOLVED_COUNT_KEY]
    assert isinstance(count, int)
    return count


def _stamped_worktree(tmp_path: Path, name: str = "wt") -> Path:
    """Create a worktree carrying a freshly-written cw-context.json."""
    worktree = tmp_path / name
    worktree.mkdir()
    _write_hook_context_file(worktree)
    return worktree


def test_pretooluse_sets_unresolved_marker(tmp_path: Path) -> None:
    """agent-spawn-pre increments the unresolved-spawn counter to 1."""
    worktree = _stamped_worktree(tmp_path)
    assert _read_count(worktree) == 0

    result = _invoke_hook_command("agent-spawn-pre", _payload(_PRE_PAYLOAD, worktree))

    assert result.exit_code == 0
    assert _read_count(worktree) == 1


def test_pretooluse_stamps_last_stamped_at(tmp_path: Path) -> None:
    """agent-spawn-pre records when the unresolved spawn began."""
    worktree = _stamped_worktree(tmp_path)

    _invoke_hook_command("agent-spawn-pre", _payload(_PRE_PAYLOAD, worktree))

    context = json.loads(
        (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert context[AGENT_SPAWN_STAMP_KEY]["last_stamped_at"] is not None


def test_pre_hook_accumulates_across_parallel_spawns(tmp_path: Path) -> None:
    """Two Pre calls before any clear reach 2 — a boolean marker would lose one.

    Claude Code can dispatch several subagent ``tool_use`` blocks in a single
    assistant turn, so two Pre hooks can fire before the next Stop clears the
    counter. The counter shape (rather than a flag) is what keeps the second
    spawn's Pre from being swallowed by the first.

    Invoked sequentially rather than from threads on purpose: ``CliRunner``
    swaps ``sys.stdin`` process-wide, so concurrent in-process invocations
    would race on the harness, not on the code under test. The lock itself is
    covered by ``test_pre_hook_fails_open_on_lock_exhaustion``.
    """
    worktree = _stamped_worktree(tmp_path)

    _invoke_hook_command("agent-spawn-pre", _payload(_PRE_PAYLOAD, worktree))
    _invoke_hook_command("agent-spawn-pre", _payload(_PRE_PAYLOAD, worktree))

    assert _read_count(worktree) == 2


def test_pre_hook_never_crashes_on_malformed_stdin() -> None:
    """Non-JSON stdin → silent exit 0 (a hook must never crash)."""
    from click.testing import CliRunner

    from cw.cli import main

    result = CliRunner().invoke(main, ["agent-spawn-pre"], input="not json at all")

    assert result.exit_code == 0


def test_pre_hook_never_crashes_on_malformed_context(tmp_path: Path) -> None:
    """A corrupt cw-context.json is left untouched, exit 0."""
    worktree = tmp_path / "broken"
    (worktree / ".claude").mkdir(parents=True)
    context_path = worktree / HOOK_CONTEXT_RELATIVE_PATH
    context_path.write_text("{ not json", encoding="utf-8")

    result = _invoke_hook_command("agent-spawn-pre", _payload(_PRE_PAYLOAD, worktree))

    assert result.exit_code == 0
    assert context_path.read_text(encoding="utf-8") == "{ not json"


def test_pre_hook_never_crashes_on_missing_cwd(tmp_path: Path) -> None:
    """A payload with no ``cwd`` key → silent exit 0."""
    payload = {k: v for k, v in _PRE_PAYLOAD.items() if k != "cwd"}

    result = _invoke_hook_command("agent-spawn-pre", payload)

    assert result.exit_code == 0


def test_pre_hook_survives_unexpected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash inside the stamp body is swallowed → exit 0 (hook never wedges)."""

    def _boom(_delta: int) -> None:
        msg = "unexpected"
        raise RuntimeError(msg)

    monkeypatch.setattr("cw.cli.agent_spawn_stamp._adjust_unresolved_count", _boom)

    result = _invoke_hook_command("agent-spawn-pre", _payload(_PRE_PAYLOAD, tmp_path))

    assert result.exit_code == 0


def test_pre_hook_fails_open_on_lock_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held per-worktree lock → bounded retry exhausts, exit 0, no stamp.

    The blast radius this guards: ``PreToolUse`` runs synchronously in the
    live worker's own turn, so a plain blocking ``LOCK_EX`` would hang the
    worker itself on contention. Exhaustion must fail open (skip the stamp),
    never block.

    Patches ``cw.cli._hook_io._LOCK_TIMEOUT_SECS_DEFAULT`` (#1947 moved the
    lock primitive there so ``cw signal-stop`` can share it) rather than the
    re-exported name on ``cw.cli.agent_spawn_stamp`` — ``_context_lock``
    reads the module-global where it is *defined*, so patching the
    re-exported copy would silently be a no-op.
    """
    import fcntl

    monkeypatch.setattr(
        "cw.cli._hook_io._LOCK_TIMEOUT_SECS_DEFAULT", 0.05, raising=True
    )
    worktree = _stamped_worktree(tmp_path)
    lock_path = worktree / ".claude" / "cw-context.json.lock"

    with lock_path.open("w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        result = _invoke_hook_command(
            "agent-spawn-pre", _payload(_PRE_PAYLOAD, worktree)
        )

    assert result.exit_code == 0
    assert _read_count(worktree) == 0


def test_context_lock_yields_true_when_uncontended(tmp_path: Path) -> None:
    """The lock helper reports acquisition on an uncontended worktree."""
    worktree = _stamped_worktree(tmp_path)

    with _context_lock(worktree / HOOK_CONTEXT_RELATIVE_PATH) as acquired:
        assert acquired is True


def test_unresolved_count_reads_legacy_context_as_zero(tmp_path: Path) -> None:
    """A pre-v5 context with no stamp key reads as 0, not an error."""
    assert _unresolved_count({}) == 0
    assert _unresolved_count({AGENT_SPAWN_STAMP_KEY: "not-a-dict"}) == 0
    assert _unresolved_count({AGENT_SPAWN_STAMP_KEY: {}}) == 0
    assert (
        _unresolved_count(
            {AGENT_SPAWN_STAMP_KEY: {AGENT_SPAWN_UNRESOLVED_COUNT_KEY: True}}
        )
        == 0
    )
    assert (
        _unresolved_count(
            {AGENT_SPAWN_STAMP_KEY: {AGENT_SPAWN_UNRESOLVED_COUNT_KEY: 3}}
        )
        == 3
    )


def test_last_stamped_at_tolerates_every_odd_shape() -> None:
    """The timestamp reader degrades to None rather than propagating junk.

    It feeds the decrement path's carry-forward, so a non-str value here would
    otherwise be written straight back into the context on the next write.
    """
    assert _last_stamped_at({}) is None
    assert _last_stamped_at({AGENT_SPAWN_STAMP_KEY: "not-a-dict"}) is None
    assert _last_stamped_at({AGENT_SPAWN_STAMP_KEY: {}}) is None
    assert _last_stamped_at({AGENT_SPAWN_STAMP_KEY: {"last_stamped_at": 7}}) is None
    assert (
        _last_stamped_at({AGENT_SPAWN_STAMP_KEY: {"last_stamped_at": "2026-01-01"}})
        == "2026-01-01"
    )


def test_lock_timeout_default_is_bounded() -> None:
    """The retry budget is small — this runs inside the worker's own turn."""
    assert 0 < _LOCK_TIMEOUT_SECS_DEFAULT <= 1.0
