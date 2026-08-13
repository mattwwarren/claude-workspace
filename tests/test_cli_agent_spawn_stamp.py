"""Tests for the ``cw agent-spawn-pre`` / ``cw agent-spawn-post`` hooks (#1646).

The pair stamps an "unresolved subagent spawn" counter into the worktree's
``.claude/cw-context.json``: ``agent-spawn-pre`` increments it when a subagent
tool call starts, ``agent-spawn-post`` decrements it when that call returns. A
crash between the two leaves the counter above zero, which the phantom sweep
reads as durable evidence that the worker died mid-spawn (rather than the
generic "surface vanished" phantom).

Both commands mirror ``cw guard-cwd``'s fail-open contract: unreadable stdin,
a missing/malformed context, or a contended lock all yield a silent exit 0.
Unlike the guard they never block a tool call at all — there is no failure mode
in which refusing a subagent spawn is the right answer.

Hook payload fixtures in this file are redacted copies of a **real** captured
``PreToolUse``/``PostToolUse`` payload (captured 2026-08-12 against Claude Code
via a temporary catch-all hook in a live dispatch worktree), not hand-authored
from prose. That capture is also what settled the matcher name: the subagent
tool reports ``"tool_name": "Agent"``, not ``"Task"``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from cw.cli.agent_spawn_stamp import (
    _LOCK_TIMEOUT_SECS_DEFAULT,
    _context_lock,
    _last_stamped_at,
    _unresolved_count,
)
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

# Redacted capture of the matching PostToolUse payload. ``tool_response`` is
# truncated to the keys present in the capture's shape rather than removed —
# the handler ignores it, and an invented-clean payload would not.
_POST_PAYLOAD: dict[str, object] = {
    **_PRE_PAYLOAD,
    "hook_event_name": "PostToolUse",
    "tool_response": {
        "status": "completed",
        "agentId": "0000000000000000b",
        "agentType": "Explore",
        "content": [{"type": "text", "text": "PROBE"}],
        "totalDurationMs": 1467,
    },
    "duration_ms": 2970,
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


def test_posttooluse_clears_unresolved_marker(tmp_path: Path) -> None:
    """Pre then Post returns the counter to 0 — the resolved-spawn happy path."""
    worktree = _stamped_worktree(tmp_path)

    _invoke_hook_command("agent-spawn-pre", _payload(_PRE_PAYLOAD, worktree))
    assert _read_count(worktree) == 1
    result = _invoke_hook_command("agent-spawn-post", _payload(_POST_PAYLOAD, worktree))

    assert result.exit_code == 0
    assert _read_count(worktree) == 0


def test_posttooluse_absent_leaves_marker_set(tmp_path: Path) -> None:
    """Only Pre runs → the marker stays set (the crash/pause signature).

    This single test covers BOTH failure shapes the ticket names — an agent
    that explicitly pauses on a subagent tool call, and one that silently drops
    it with no ``tool_result`` ever recorded. The hook layer can observe
    neither prose nor tool results; it only ever sees whether its own Post
    fired, so both shapes produce this identical on-disk signature.
    """
    worktree = _stamped_worktree(tmp_path)

    _invoke_hook_command("agent-spawn-pre", _payload(_PRE_PAYLOAD, worktree))

    assert _read_count(worktree) == 1


def test_pre_hook_accumulates_across_parallel_spawns(tmp_path: Path) -> None:
    """Two Pre calls before any Post reach 2 — a boolean marker would lose one.

    Claude Code can dispatch several subagent ``tool_use`` blocks in a single
    assistant turn, so two Pre hooks can fire before either Post does. The
    counter shape (rather than a flag) is what keeps the second spawn's Pre
    from being swallowed by the first's Post.

    Invoked sequentially rather than from threads on purpose: ``CliRunner``
    swaps ``sys.stdin`` process-wide, so concurrent in-process invocations
    would race on the harness, not on the code under test. The lock itself is
    covered by ``test_pre_hook_fails_open_on_lock_exhaustion``.
    """
    worktree = _stamped_worktree(tmp_path)

    _invoke_hook_command("agent-spawn-pre", _payload(_PRE_PAYLOAD, worktree))
    _invoke_hook_command("agent-spawn-pre", _payload(_PRE_PAYLOAD, worktree))

    assert _read_count(worktree) == 2


def test_post_hook_floors_counter_at_zero(tmp_path: Path) -> None:
    """A Post with no matching prior Pre must not drive the counter negative."""
    worktree = _stamped_worktree(tmp_path)

    result = _invoke_hook_command("agent-spawn-post", _payload(_POST_PAYLOAD, worktree))

    assert result.exit_code == 0
    assert _read_count(worktree) == 0


def test_pre_hook_never_crashes_on_malformed_stdin() -> None:
    """Non-JSON stdin → silent exit 0 (a hook must never crash)."""
    from click.testing import CliRunner

    from cw.cli import main

    result = CliRunner().invoke(main, ["agent-spawn-pre"], input="not json at all")

    assert result.exit_code == 0


def test_post_hook_never_crashes_on_missing_context(tmp_path: Path) -> None:
    """No cw-context.json under cwd → silent exit 0, nothing created."""
    bare = tmp_path / "bare"
    bare.mkdir()

    result = _invoke_hook_command("agent-spawn-post", _payload(_POST_PAYLOAD, bare))

    assert result.exit_code == 0
    assert not (bare / HOOK_CONTEXT_RELATIVE_PATH).exists()


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
    """
    import fcntl

    monkeypatch.setattr(
        "cw.cli.agent_spawn_stamp._LOCK_TIMEOUT_SECS_DEFAULT", 0.05, raising=True
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
    otherwise be written straight back into the context on the next Post.
    """
    assert _last_stamped_at({}) is None
    assert _last_stamped_at({AGENT_SPAWN_STAMP_KEY: "not-a-dict"}) is None
    assert _last_stamped_at({AGENT_SPAWN_STAMP_KEY: {}}) is None
    assert _last_stamped_at({AGENT_SPAWN_STAMP_KEY: {"last_stamped_at": 7}}) is None
    assert (
        _last_stamped_at({AGENT_SPAWN_STAMP_KEY: {"last_stamped_at": "2026-01-01"}})
        == "2026-01-01"
    )


def test_post_hook_survives_unexpected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Post handler swallows a crash too — same contract as Pre.

    Asserted separately rather than assumed from the Pre test: the two
    commands carry their own independent try/except, so one could lose it
    without the other's test noticing.
    """

    def _boom(_delta: int) -> None:
        msg = "unexpected"
        raise RuntimeError(msg)

    monkeypatch.setattr("cw.cli.agent_spawn_stamp._adjust_unresolved_count", _boom)

    result = _invoke_hook_command("agent-spawn-post", _payload(_POST_PAYLOAD, tmp_path))

    assert result.exit_code == 0


def test_post_hook_preserves_last_stamped_at_on_decrement(tmp_path: Path) -> None:
    """Decrement carries the original timestamp forward, it does not refresh it.

    ``last_stamped_at`` answers "when did the oldest unresolved spawn begin",
    which is what an operator reading a parked row needs; refreshing it on the
    way down would erase exactly that.
    """
    worktree = _stamped_worktree(tmp_path)

    _invoke_hook_command("agent-spawn-pre", _payload(_PRE_PAYLOAD, worktree))
    _invoke_hook_command("agent-spawn-pre", _payload(_PRE_PAYLOAD, worktree))
    context = json.loads(
        (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    stamped_at = context[AGENT_SPAWN_STAMP_KEY]["last_stamped_at"]

    _invoke_hook_command("agent-spawn-post", _payload(_POST_PAYLOAD, worktree))

    context = json.loads(
        (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert context[AGENT_SPAWN_STAMP_KEY]["last_stamped_at"] == stamped_at
    assert _read_count(worktree) == 1


def test_lock_timeout_default_is_bounded() -> None:
    """The retry budget is small — this runs inside the worker's own turn."""
    assert 0 < _LOCK_TIMEOUT_SECS_DEFAULT <= 1.0
