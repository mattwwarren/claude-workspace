"""Tests for ``cw event session-outcomes`` in cw.cli.queues (#2840).

NEW file, 1:1 with ``src/cw/cli/queues.py``. Fixture inventory: ``CliRunner``,
autouse ``tmp_config_dir`` + ``_isolate_home``, ``cw.events.record_event`` for
seeding, ``tests.conftest._make_daemon_session`` + ``_seed_sessions`` for the
live-session case, and a raw write to ``inbox_path()`` for corrupt-line cases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from cw.cli import main
from cw.events import inbox_path, record_event
from cw.models import OrchestratorEventType, SessionStatus
from tests.conftest import _make_daemon_session, _seed_sessions


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(main, ["event", "session-outcomes", *args])


def _seed_spawn_and_crash(sid: str, client: str = "acme") -> None:
    record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"session_id": sid, "ticket_id": f"T-{sid}", "client": client},
    )
    record_event(
        OrchestratorEventType.SESSION_COMPLETED,
        {"session_id": sid, "crashed": True},
    )


def test_json_output_has_schema_and_counts() -> None:
    _seed_spawn_and_crash("s1")
    result = _invoke("--days", "42", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["spawned"] == 1
    assert payload["without_result"] == 1
    assert payload["largest_cause"] == "crash"


def test_text_output_has_headline_and_largest_line() -> None:
    _seed_spawn_and_crash("s1")
    result = _invoke()
    assert result.exit_code == 0
    output = result.output
    assert "Worker sessions spawned: 1" in output
    assert "Largest failure cause: crash (1)" in output


@pytest.mark.parametrize(
    "args",
    [
        ("-c", "acme", "-c", "other"),
        ("-c", "acme,other"),
    ],
)
def test_client_filter_accepts_repeat_and_comma(args: tuple[str, ...]) -> None:
    _seed_spawn_and_crash("s1", client="acme")
    _seed_spawn_and_crash("s2", client="other")
    _seed_spawn_and_crash("s3", client="third")
    result = _invoke(*args, "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["spawned"] == 2


def test_single_client_filter_limits_spawns() -> None:
    _seed_spawn_and_crash("s1", client="acme")
    _seed_spawn_and_crash("s2", client="other")
    result = _invoke("-c", "acme", "--json")
    payload = json.loads(result.output)
    assert payload["spawned"] == 1


@pytest.mark.parametrize("bad", ["0", "-3"])
def test_days_must_be_positive(bad: str) -> None:
    result = _invoke("--days", bad)
    assert result.exit_code == 2


def test_samples_option_is_honoured() -> None:
    for sid in ("s1", "s2", "s3"):
        _seed_spawn_and_crash(sid)
    result = _invoke("--samples", "1", "--json")
    payload = json.loads(result.output)
    crash = next(b for b in payload["buckets"] if b["cause"] == "crash")
    assert crash["count"] == 3
    assert len(crash["sample_session_ids"]) == 1


def test_empty_inbox_prints_zero_spawn_message() -> None:
    result = _invoke()
    assert result.exit_code == 0
    assert "no worker sessions spawned in window" in result.output


def test_live_session_seeded_in_state_is_in_flight() -> None:
    record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"session_id": "live1", "ticket_id": "T-live", "client": "acme"},
    )
    _seed_sessions(_make_daemon_session(id="live1", status=SessionStatus.ACTIVE))
    result = _invoke("--json")
    payload = json.loads(result.output)
    in_flight = next(b for b in payload["buckets"] if b["cause"] == "in_flight")
    assert in_flight["count"] == 1
    assert payload["largest_cause"] is None


def _assert_clean_error(result: Result, class_name: str) -> None:
    output = result.output
    assert result.exit_code == 1
    assert output.startswith("Error:")
    assert class_name in output
    assert "Traceback" not in output
    assert not isinstance(result.exception, json.JSONDecodeError)


def test_interior_corrupt_inbox_line_is_clean_error() -> None:
    record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"session_id": "s1", "ticket_id": "T-1", "client": "acme"},
    )
    path: Path = inbox_path()
    with path.open("a") as handle:
        handle.write("{not json\n")
    record_event(
        OrchestratorEventType.SESSION_SPAWNED,
        {"session_id": "s2", "ticket_id": "T-2", "client": "acme"},
    )
    result = _invoke()
    _assert_clean_error(result, "JSONDecodeError")


def test_read_oserror_is_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        msg = "inbox gone"
        raise OSError(msg)

    monkeypatch.setattr("cw.session_outcomes.read_events", _boom)
    result = _invoke()
    _assert_clean_error(result, "OSError")


def test_corrupt_sessions_json_is_clean_error() -> None:
    from cw.config import STATE_FILE

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text("{not json")
    result = _invoke()
    _assert_clean_error(result, "JSONDecodeError")
