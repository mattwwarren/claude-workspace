#!/usr/bin/env python3
"""Block until an attention event arrives for CLIENT, print it, exit.

Wake-on-event watch for the orchestrate-sprint skill (Phase 4), run under a
backgrounded ``Bash`` (``run_in_background: true``). The harness sends one
completion notification when this script exits, so the orchestrator wakes only
when there is something to triage, then re-arms. It replaces the old
Monitor-armed ``attention_monitor.sh``: the Monitor tool has no persistent
mode and expires within 30 minutes, and each re-arm of that script restarted
at ``--since now``, dropping every event created between arms (#2250).

How one arm works:

- Resume from a per-client stamp file (``--stamp-path``, default
  ``~/.claude-workspace/attention-stamp-<client>[-<lane>].json``) holding the
  last consumed ``created_at``, truncated to the second to match ``cw event
  tail --since`` granularity, plus the event ids already consumed in that
  second. The ids dedup the replayed second, so a re-arm neither replays an
  event forever nor drops a same-second one. ``--since`` overrides the stamp.
- Compare timestamps on their first 19 characters only. The bus serializes
  ``created_at`` with microseconds and a ``Z``, and ``"...:58.955Z" <
  "...:58Z"`` is true as strings.
- Read ``cw event tail --follow`` on a reader thread feeding a queue:
  ``select()`` on a text-mode pipe followed by ``readline()`` can leave lines
  stranded in Python's buffer.
- After the first rendered event, drain a ``BURST_S`` window so a cluster of
  events is one wake, not several; then terminate ``cw event tail``
  explicitly (a ``| head -1`` style pipeline would hang until its next write).
- If ``cw event tail`` exits on its own, print ``WATCHER | cw event tail
  exited rc=N`` so a dead watch pages instead of going quiet.
- If nothing qualifying arrives within ``--max-idle-seconds`` (default 7200),
  print ``WATCHER | idle backstop after <N>s, no events, re-arm`` and exit 0.
  Nothing documents how long the harness keeps a background ``Bash`` alive,
  so this bounds silence regardless.

Events that are consumed but not rendered (e.g. a ``live`` liveness flap)
still advance the stamp; events still queued when the burst window closes do
not, so the next arm replays them.

Cross-arm dedup is deliberately limited to the stamp's ``created_at`` +
``ids``. ``--dedup-terminal`` and the liveness-bucket latch below only
collapse repeats within one arm. A park that re-fires after a re-arm carries a
new event id and pages again; that is intended -- the orchestrator triages
before it re-arms, so a repeat means the row is still parked.

Standalone by design: no ``cw`` import, so it runs under a bare ``python3``.

Usage: attention_watch.py [CLIENT] [LANE] [--since TS] [--stamp-path PATH]
                          [--cw-bin PATH] [--max-idle-seconds N]
  CLIENT defaults to claude-workspace. LANE scopes the stream to one lane and
  exists only for two orchestrators sharing one client -- a lone orchestrator
  that passes a lane silently loses every other lane's events.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

DEFAULT_CLIENT = "claude-workspace"
BURST_S = 3.0
DEFAULT_MAX_IDLE_SECONDS = 7200.0
UPSTREAM_STOP_TIMEOUT_S = 5.0
# len("YYYY-MM-DDTHH:MM:SS") -- the second-granularity prefix of created_at.
SECOND_PREFIX_LEN = 19

LIVENESS_EVENT_TYPE = "session.liveness_changed"

TYPES = [
    "session.needs_attention",
    "operator.escalation",
    "session.timed_out",
    "session.reap_proposed",
    "session.stage_timed_out_retried",
    "session.phantom_reverted",
    LIVENESS_EVENT_TYPE,
]

# Only these LivenessBucket values page (#2004). "live"/"stale_15m" flap during
# a legitimate quiet stretch (~20m review stage, #1795); surfacing them teaches
# operators to ignore this channel. Per-stage entry thresholds mean stale_30m
# is not a fixed duration, so render stale_minutes, never imply one.
SURFACED_LIVENESS = frozenset({"stale_30m", "stale_45m"})

# Hand-copy of cw.dispatch.BREADCRUMB_ELIGIBLE_PAUSED_STATUSES (this script
# cannot import cw); pinned by tests/test_attention_watch.py. Only these
# paused_status values carry blocker.reason verbatim in breadcrumbs -- for
# every other producer breadcrumbs is polymorphic (worktree paths, fixed
# strings), so this is a gated allowlist, not a blanket fallback (#1597).
BLOCKER_REASON_PAUSED_STATUSES = frozenset(
    {
        "blocked",
        "awaiting_operator_availability",
        "merge_gate_blocked",
        "codex_must_fix_mechanically_rejected",
        "empty_diff_blocked",
        "stale_dispatch",
    }
)
# A companion diagnostic (#1717), not a blocker.reason carrier: its breadcrumbs
# is a composite "attempts=... branch_head=..." string, always worth showing.
FINALIZE_REGRESS_REPEAT_PAUSED_STATUS = "finalize_regress_repeat"


def default_stamp_path(client: str, lane: str | None = None) -> Path:
    """Per-client (and per-lane, when scoped) resume stamp location.

    ``~/.claude-workspace`` mirrors ``cw.config.ORCHESTRATOR_CONFIG_DIR``.
    """
    suffix = f"-{lane}" if lane else ""
    return Path.home() / ".claude-workspace" / f"attention-stamp-{client}{suffix}.json"


def load_stamp(stamp_path: Path) -> tuple[str, set[str]]:
    """Return ``(created_at, ids)`` from the stamp, or ``(now, set())``."""
    try:
        data = json.loads(stamp_path.read_text())
        return str(data["created_at"]), {str(i) for i in data.get("ids", [])}
    except (OSError, ValueError, KeyError, TypeError):
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return now, set()


def save_stamp(stamp_path: Path, created_at: str, ids: set[str]) -> None:
    """Write the stamp atomically (temp file + rename).

    A torn write would make :func:`load_stamp` fall back to "now" and silently
    drop every event between the last delivery and the re-arm -- the exact gap
    this script exists to close (#2250).
    """
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = stamp_path.with_name(f"{stamp_path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"created_at": created_at, "ids": sorted(ids)}))
    tmp.replace(stamp_path)


class ResumeState:
    """Stamp-driven dedup: which events are new, and where the stamp moves."""

    def __init__(self, since: str, seen: set[str]) -> None:
        self.since = since
        self.seen = set(seen)
        self.last_created = since
        # Only ids at the resume second matter for replay dedup.
        self.sec_ids = set(seen)

    def accept(self, event: dict[str, Any]) -> bool:
        """Consume ``event`` if new; return False for a replay or a pre-since event."""
        eid = str(event.get("id", ""))
        second = str(event.get("created_at", ""))[:SECOND_PREFIX_LEN]
        if eid in self.seen or second < self.since[:SECOND_PREFIX_LEN]:
            return False
        self.seen.add(eid)
        if second > self.last_created[:SECOND_PREFIX_LEN]:
            self.last_created = second + "Z"
            self.sec_ids.clear()
        if second == self.last_created[:SECOND_PREFIX_LEN]:
            self.sec_ids.add(eid)
        return True


def _suppress_liveness(payload: dict[str, Any], latch: dict[str, str]) -> bool:
    """True when a liveness event must not page; updates the per-session latch.

    ``--dedup-terminal`` does not cover liveness events (its key has no
    bucket), so repeats of the same surfaced bucket are collapsed here. A
    recovery clears the latch, so a genuine re-stall into the same bucket
    pages again.
    """
    sid = str(payload.get("session_id") or "")
    bucket = payload.get("new_bucket")
    if bucket not in SURFACED_LIVENESS:
        latch.pop(sid, None)
        return True
    if latch.get(sid) == bucket:
        return True
    latch[sid] = str(bucket)
    return False


def fmt(event: dict[str, Any], latch: dict[str, str]) -> str | None:
    """Render one event as an ``ATTENTION |`` line, or None to suppress it."""
    p: dict[str, Any] = event.get("payload") or {}
    etype = event.get("type", "?")
    if etype == LIVENESS_EVENT_TYPE and _suppress_liveness(p, latch):
        return None
    why = (
        p.get("paused_status")
        or p.get("reason")
        or p.get("proposed_action")
        or p.get("new_bucket")
        or ""
    )
    bits = []
    if p.get("stage"):
        bits.append(f"stage={p['stage']}")
    if p.get("attempts") is not None:
        bits.append(f"att={p['attempts']}")
    if p.get("stale_minutes") is not None:
        bits.append(f"stale_m={float(p['stale_minutes']):.1f}")
    if p.get("lane"):
        bits.append(f"lane={p['lane']}")
    paused = p.get("paused_status")
    if p.get("breadcrumbs") and (
        paused in BLOCKER_REASON_PAUSED_STATUSES
        or paused == FINALIZE_REGRESS_REPEAT_PAUSED_STATUS
    ):
        bits.append(f"reason={p['breadcrumbs']}")
    created = str(event.get("created_at", ""))[:SECOND_PREFIX_LEN]
    ticket = p.get("ticket_id") or "?"
    sess = str(p.get("session_id") or "")[:8]
    fields = [created, str(etype), f"#{ticket}", str(why), " ".join(bits), sess]
    return "ATTENTION | " + " | ".join(fields)


@dataclass
class Wake:
    """Why one arm ended, and the lines to print for it."""

    lines: list[str] = field(default_factory=list)
    upstream_exited: bool = False
    idle_backstop: bool = False


def drain(
    lines_q: queue.Queue[str | None], state: ResumeState, *, max_idle_s: float
) -> Wake:
    """Consume queued ``cw event tail`` lines until a wake condition fires.

    ``None`` on the queue means the upstream process closed its stdout.
    """
    wake = Wake()
    latch: dict[str, str] = {}
    idle_deadline = time.monotonic() + max_idle_s
    burst_deadline: float | None = None
    while True:
        deadline = idle_deadline if burst_deadline is None else burst_deadline
        try:
            raw = lines_q.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            if burst_deadline is None:
                wake.idle_backstop = True
                wake.lines.append(
                    f"WATCHER | idle backstop after {max_idle_s:g}s, no events, re-arm"
                )
            return wake
        if raw is None:
            wake.upstream_exited = True
            return wake
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict) or not state.accept(event):
            continue
        line = fmt(event, latch)
        if line is None:
            continue
        wake.lines.append(line)
        if burst_deadline is None:
            burst_deadline = time.monotonic() + BURST_S


def build_command(cw_bin: str, client: str, lane: str | None, since: str) -> list[str]:
    cmd = [cw_bin, "event", "tail", "--follow", "--client", client]
    cmd += ["--dedup-terminal", "--since", since, "--json"]
    if lane:
        cmd += ["--lane", lane]
    for etype in TYPES:
        cmd += ["--type", etype]
    return cmd


def _start_reader(stdout: IO[str] | None) -> queue.Queue[str | None]:
    lines_q: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        for raw in stdout or ():
            lines_q.put(raw)
        lines_q.put(None)

    threading.Thread(target=pump, daemon=True).start()
    return lines_q


def _stop(proc: subprocess.Popen[str]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=UPSTREAM_STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wake-on-event attention watch for one cw client."
    )
    parser.add_argument("client", nargs="?", default=DEFAULT_CLIENT)
    parser.add_argument(
        "lane",
        nargs="?",
        default=None,
        help="Scope to one lane. Only for two orchestrators sharing a client.",
    )
    parser.add_argument(
        "--since", help="ISO timestamp to start from; bypasses the stamp."
    )
    parser.add_argument("--stamp-path", type=Path, help="Resume stamp file location.")
    parser.add_argument(
        "--cw-bin",
        default=os.environ.get("CW_BIN", "cw"),
        help="cw executable (default: $CW_BIN, else cw on PATH).",
    )
    parser.add_argument(
        "--max-idle-seconds",
        type=float,
        default=DEFAULT_MAX_IDLE_SECONDS,
        help="Exit with a WATCHER backstop line if no event arrives by then.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    stamp_path: Path = args.stamp_path or default_stamp_path(args.client, args.lane)
    since, seen = load_stamp(stamp_path)
    if args.since:
        since, seen = args.since, set()
    state = ResumeState(since, seen)
    cmd = build_command(args.cw_bin, args.client, args.lane, since)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    try:
        wake = drain(
            _start_reader(proc.stdout), state, max_idle_s=args.max_idle_seconds
        )
        if wake.upstream_exited:
            wake.lines.append(f"WATCHER | cw event tail exited rc={proc.wait()}")
    finally:
        _stop(proc)
    print("\n".join(wake.lines), flush=True)
    # Only after printing: an arm interrupted mid-drain must replay what it
    # consumed, not advance the stamp past lines nobody saw.
    save_stamp(stamp_path, state.last_created, state.sec_ids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
