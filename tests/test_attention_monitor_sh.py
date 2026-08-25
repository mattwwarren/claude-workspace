"""Guard tests for `attention_monitor.sh`'s liveness-bucket subscription (#2004).

`.claude/skills/orchestrate-sprint/scripts/attention_monitor.sh` streams the
`cw event tail` bus, filtered to a fixed set of attention-worthy event types
and rendered by an embedded `python3 -u -c '...'` filter. Before this ticket,
`session.liveness_changed` was absent from the `--type` subscription list and
the embedded filter had no bucket-aware branch at all -- a session that
spawned and then stalled (no failure event ever fires) was invisible to the
monitor until it hit a hard timeout. See docs/dispatch-runbook.md §4.0 for
the "two kinds of monitoring" background this closes.

`cw event tail --follow` itself is unsuited to a unit test (it blocks, hits
the real event bus, and needs live daemon/roster state), so these tests
extract the embedded `python3 -u -c '...'` filter body directly out of the
shell script and execute it as a standalone script against synthetic
JSON-lines on stdin, bypassing `cw event tail` entirely. Structure mirrors
the "subprocess-driven shell-exercise convention" in `tests/test_release_sh.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
ATTENTION_MONITOR_SH = (
    ROOT
    / ".claude"
    / "skills"
    / "orchestrate-sprint"
    / "scripts"
    / "attention_monitor.sh"
)


def _extract_embedded_filter() -> str:
    """Pull the single-quoted `python3 -u -c '...'` body out of the script.

    Kept deliberately naive (index/slice, not a shell parser): the script's
    own docstring (lines 18-20) states this Python is intentionally boring
    and single-quoted with no nested quotes, specifically so it can be
    copy-pasted whole into a Monitor command string -- the same property
    that makes simple string slicing a reliable extraction here.
    """
    text = ATTENTION_MONITOR_SH.read_text()
    marker = "python3 -u -c '"
    start = text.index(marker) + len(marker)
    body = text[start:]
    assert body.rstrip().endswith("'"), "embedded python block must be single-quoted"
    return body.rstrip()[:-1]


def _run_filter(lines: list[dict[str, object]]) -> list[str]:
    code = _extract_embedded_filter()
    stdin = "\n".join(json.dumps(line) for line in lines) + "\n"
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return [ln for ln in result.stdout.splitlines() if ln]


def _liveness_event(
    session_id: str = "sess-abcdef1234567890",
    ticket_id: str = "T-123",
    client: str = "claude-workspace",
    stage: str = "impl",
    old_bucket: str = "stale_15m",
    new_bucket: str = "stale_30m",
    stale_minutes: float = 32.4,
) -> dict[str, object]:
    return {
        "type": "session.liveness_changed",
        "payload": {
            "session_id": session_id,
            "ticket_id": ticket_id,
            "client": client,
            "stage": stage,
            "old_bucket": old_bucket,
            "new_bucket": new_bucket,
            "stale_minutes": stale_minutes,
        },
    }


def test_shell_syntax_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(ATTENTION_MONITOR_SH)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_subscribes_to_liveness_changed_type() -> None:
    text = ATTENTION_MONITOR_SH.read_text()
    assert "--type session.liveness_changed" in text
    for existing in (
        "--type session.needs_attention",
        "--type operator.escalation",
        "--type session.timed_out",
        "--type session.reap_proposed",
        "--type session.stage_timed_out_retried",
        "--type session.phantom_reverted",
    ):
        assert existing in text


def test_dedup_terminal_flag_still_present() -> None:
    text = ATTENTION_MONITOR_SH.read_text()
    assert "--dedup-terminal" in text


def test_liveness_live_bucket_suppressed() -> None:
    out = _run_filter([_liveness_event(new_bucket="live")])
    assert out == []


def test_liveness_stale_15m_suppressed() -> None:
    out = _run_filter([_liveness_event(new_bucket="stale_15m")])
    assert out == []


def test_liveness_stale_30m_surfaced() -> None:
    out = _run_filter(
        [_liveness_event(new_bucket="stale_30m", stale_minutes=32.4, stage="impl")]
    )
    assert len(out) == 1
    line = out[0]
    assert "T-123" in line
    assert "impl" in line
    assert "stale_30m" in line
    assert "32.4" in line


def test_liveness_stale_45m_surfaced() -> None:
    out30 = _run_filter([_liveness_event(new_bucket="stale_30m", stale_minutes=32.4)])
    out45 = _run_filter(
        [
            _liveness_event(
                session_id="sess-other000000000",
                new_bucket="stale_45m",
                stale_minutes=47.1,
            )
        ]
    )
    assert len(out45) == 1
    assert "stale_45m" in out45[0]
    assert out30[0] != out45[0]


def test_liveness_per_stage_duration_not_implied() -> None:
    out = _run_filter(
        [
            _liveness_event(
                stage="impl",
                new_bucket="stale_30m",
                stale_minutes=34.9,
            )
        ]
    )
    assert len(out) == 1
    assert "34.9" in out[0]
    assert "35" not in out[0].replace("34.9", "")


def test_liveness_repeat_same_bucket_suppressed() -> None:
    events = [
        _liveness_event(session_id="sess-same0000000000", new_bucket="stale_30m"),
        _liveness_event(session_id="sess-same0000000000", new_bucket="stale_30m"),
    ]
    out = _run_filter(events)
    assert len(out) == 1


def test_liveness_escalation_always_surfaced() -> None:
    events = [
        _liveness_event(
            session_id="sess-esc00000000000", new_bucket="stale_30m", stale_minutes=31.0
        ),
        _liveness_event(
            session_id="sess-esc00000000000", new_bucket="stale_45m", stale_minutes=46.0
        ),
    ]
    out = _run_filter(events)
    assert len(out) == 2
    assert "stale_30m" in out[0]
    assert "stale_45m" in out[1]


def test_liveness_deescalation_then_reescalation_both_surfaced() -> None:
    sid = "sess-knife00000000"
    events = [
        _liveness_event(session_id=sid, new_bucket="stale_45m"),
        _liveness_event(session_id=sid, new_bucket="stale_30m"),
        _liveness_event(session_id=sid, new_bucket="stale_45m"),
    ]
    out = _run_filter(events)
    assert len(out) == 3


def test_liveness_recovery_then_restall_same_bucket_both_surfaced() -> None:
    sid = "sess-recover0000000"
    events = [
        _liveness_event(session_id=sid, new_bucket="stale_30m"),
        _liveness_event(session_id=sid, new_bucket="live"),
        _liveness_event(session_id=sid, new_bucket="stale_30m"),
    ]
    out = _run_filter(events)
    assert len(out) == 2


def test_liveness_distinct_sessions_not_cross_suppressed() -> None:
    events = [
        _liveness_event(session_id="sess-aaaa00000000000", new_bucket="stale_30m"),
        _liveness_event(session_id="sess-bbbb00000000000", new_bucket="stale_30m"),
    ]
    out = _run_filter(events)
    assert len(out) == 2


def test_needs_attention_rendering_unchanged() -> None:
    event: dict[str, object] = {
        "type": "session.needs_attention",
        "payload": {
            "ticket_id": "T-999",
            "paused_status": "blocked",
            "breadcrumbs": "some breadcrumb text",
            "stage": "review",
            "attempts": 3,
            "session_id": "sess-needsattn00000",
        },
    }
    out = _run_filter([event])
    assert len(out) == 1
    line = out[0]
    assert line.startswith("ATTENTION | session.needs_attention | T-999 | blocked |")
    assert "stage=review" in line
    assert "att=3" in line
    assert "reason=some breadcrumb text" in line


def test_unknown_type_passthrough_unaffected() -> None:
    event: dict[str, object] = {
        "type": "operator.escalation",
        "payload": {
            "ticket_id": "T-77",
            "reason": "escalated",
        },
    }
    out = _run_filter([event])
    assert len(out) == 1
    assert "T-77" in out[0]
    assert "escalated" in out[0]


def test_liveness_malformed_bucket_value_not_crashing() -> None:
    out_missing = _run_filter(
        [
            {
                "type": "session.liveness_changed",
                "payload": {"session_id": "sess-x", "ticket_id": "T-1"},
            }
        ]
    )
    assert out_missing == []

    out_unknown = _run_filter([_liveness_event(new_bucket="stale_99m")])
    assert out_unknown == []


def test_docstring_mentions_liveness_rationale() -> None:
    text = ATTENTION_MONITOR_SH.read_text()
    assert "liveness" in text.lower()
