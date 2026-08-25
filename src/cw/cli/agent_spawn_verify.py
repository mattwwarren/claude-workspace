"""The ``cw agent-spawn-verify`` command (#2012).

Closes the fix-loop async-dispatch wedge from the *dispatching* side. When
``auto-dev-review.md`` Step 3b spawns its fix agent, the spawn is async: the
orchestrating session ends its turn and resumes on the subagent's completion
notification. If that spawn silently never launches, no notification ever
arrives and the session waits forever — the failure mode that produced four
reproductions before anything noticed.

This command turns that unbounded wait into a bounded check the orchestrator
runs **in the same turn as the spawn**: a real subagent writes its own
transcript into this worktree's Claude project dir, so the appearance of a
``*.jsonl`` that is neither the caller's own nor a pre-existing sibling is
cheap positive evidence the dispatch is real. Exit 0 means "verified, safe to
end the turn and await"; exit 1 means "this dispatch produced nothing" and the
stage must fail loudly (``blocker.reason: fix_loop_dispatch_unverified``).

**One Bash call, not a poll loop.** The waiting happens inside this process,
deliberately: the orchestrator issuing repeated verification calls is exactly
the busy-wait pattern ``cw guard-busy-wait`` exists to refuse.

**No fail-open contract.** Unlike the hook handlers (``cw guard-cwd``, ``cw
agent-spawn-pre``), which must never block a tool call and so exit 0 on every
ambiguity, this is an orchestrator-facing verifier: an unreadable or absent
project dir is a *verification failure*, not a pass. Exiting 0 on ambiguous
evidence would silently restore the very wedge this exists to prevent.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import click

from cw._transcript import find_new_subagent_transcript
from cw._util import claude_project_dir
from cw.cli._base import SESSION_ENV_VAR, handle_errors, main
from cw.config import load_orchestrator_config
from cw.exceptions import CwError

__all__ = ["agent_spawn_verify"]

# Floor on the per-iteration sleep. A configured interval of 0 would otherwise
# spin the CPU for the whole window; a small floor keeps a 0/absent interval
# harmless without materially delaying detection.
_MIN_POLL_SLEEP_SECONDS = 0.05

_SINCE_HELP = (
    "ISO 8601 timestamp captured immediately BEFORE the spawn. Only "
    "transcripts written after it count as this dispatch's."
)
_EXCLUDE_HELP = (
    f"Session id whose own transcript must not count as verification "
    f"(default: ${SESSION_ENV_VAR}). Without it the caller's own "
    f"actively-growing transcript trivially self-verifies."
)


def _parse_since(since: str) -> datetime:
    """Parse ``--since`` as an ISO 8601 timestamp, defaulting a naive value to UTC.

    Naive values are assumed UTC rather than rejected: the documented capture
    idiom is ``date -u +%Y-%m-%dT%H:%M:%SZ``, and an operator invoking this by
    hand should not have a missing offset turn into a verification failure.
    """
    try:
        parsed = datetime.fromisoformat(since)
    except ValueError as exc:
        msg = f"Cannot parse --since value '{since}' as an ISO 8601 timestamp."
        raise CwError(msg) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _resolve_windows(
    poll_seconds: int | None, poll_interval_seconds: int | None
) -> tuple[int, int]:
    """Resolve the poll window/interval: CLI override over orchestrator config.

    Same layering as ``cw guard-busy-wait``'s ``_resolve_settings``: read the
    global config fresh on every invocation (each call is its own subprocess,
    so an operator's edit takes effect immediately), then let an explicit flag
    win for this invocation only. The window is config-driven rather than a
    code constant because it sits on the fix-loop dispatch path every client
    runs through, where host/load variance (cold model start, contended host,
    network-mounted worktree) can otherwise false-positive a healthy dispatch.
    """
    global_cfg = load_orchestrator_config()
    window = (
        poll_seconds
        if poll_seconds is not None
        else global_cfg.agent_spawn_verify_poll_seconds
    )
    interval = (
        poll_interval_seconds
        if poll_interval_seconds is not None
        else global_cfg.agent_spawn_verify_poll_interval_seconds
    )
    return window, interval


@main.command(name="agent-spawn-verify")
@click.option("--since", required=True, help=_SINCE_HELP)
@click.option("--exclude-session", default=None, help=_EXCLUDE_HELP)
@click.option(
    "--worktree",
    default=None,
    help="Worktree whose Claude project dir is checked (default: cwd).",
)
@click.option(
    "--poll-seconds",
    type=int,
    default=None,
    help="Override agent_spawn_verify_poll_seconds for this invocation.",
)
@click.option(
    "--poll-interval-seconds",
    type=int,
    default=None,
    help="Override agent_spawn_verify_poll_interval_seconds for this invocation.",
)
@handle_errors
def agent_spawn_verify(
    since: str,
    exclude_session: str | None,
    worktree: str | None,
    poll_seconds: int | None,
    poll_interval_seconds: int | None,
) -> None:
    """Verify an async subagent spawn actually produced a transcript.

    Prints the verifying transcript path and exits 0 on success; exits 1 with
    a diagnostic naming the checked project dir, ``--since``, the exclusion,
    and the effective poll window when the window elapses with no candidate.
    """
    since_ts = _parse_since(since)
    worktree_path = Path(worktree).expanduser() if worktree else Path.cwd()
    exclude_stem = exclude_session or os.environ.get(SESSION_ENV_VAR) or None
    window, interval = _resolve_windows(poll_seconds, poll_interval_seconds)
    project_dir = claude_project_dir(worktree_path)

    started = time.monotonic()
    while True:
        found = find_new_subagent_transcript(
            project_dir, since=since_ts, exclude_stem=exclude_stem
        )
        if found is not None:
            click.echo(str(found))
            return
        remaining = window - (time.monotonic() - started)
        if remaining <= 0:
            break
        time.sleep(min(max(interval, _MIN_POLL_SLEEP_SECONDS), remaining))

    elapsed = time.monotonic() - started
    excluded = exclude_stem or "<none>"
    msg = (
        f"No new subagent transcript appeared in {project_dir} since {since} "
        f"(excluding session {excluded}) after {elapsed:.1f}s "
        f"(poll window {window}s, interval {interval}s). The spawn produced no "
        f"subagent transcript — do not end the turn awaiting a completion "
        f"notification that will not arrive."
    )
    raise CwError(msg)
