"""``cw event record`` derives a stage event's session_id from cw-context (#2651)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from cw.cli import main
from cw.cli import queues as queues_mod
from cw.events import _STAGE_EVENT_TYPES, read_events
from tests._cli_review_helpers import _write_session_context

_PAYLOAD_BASE = {"ticket_id": "173", "stage": "s2_impl_started"}
_NO_SESSION_WARNING = (
    "warning: stage event recorded without a session_id: no cw-context.json"
    " session_id was found from the working directory upward and the payload"
    " supplies none\n"
)


def _record(payload: dict[str, object], event: str = "stage.entered") -> Result:
    return CliRunner().invoke(
        main,
        ["event", "record", event, "--payload", json.dumps(payload)],
        catch_exceptions=False,
    )


@pytest.fixture
def ctx_dir(
    tmp_path: Path, tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A cwd whose nearest cw-context carries session_id ``ctx-1``."""
    _write_session_context(tmp_path, headless=True, session_id="ctx-1")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize("value", [None, ""])
def test_missing_or_empty_session_id_is_filled(ctx_dir: Path, value: object) -> None:
    payload: dict[str, object] = dict(_PAYLOAD_BASE)
    if value is not None:
        payload["session_id"] = value
    result = _record(payload)
    assert result.exit_code == 0
    assert result.stderr == ""
    assert read_events()[-1].payload["session_id"] == "ctx-1"


def test_disagreeing_session_id_is_overridden_with_warning(ctx_dir: Path) -> None:
    result = _record({**_PAYLOAD_BASE, "session_id": "X"})
    assert result.exit_code == 0
    assert result.stderr == (
        "warning: payload session_id 'X' disagrees with cw-context.json"
        " session_id 'ctx-1'; recording 'ctx-1'\n"
    )
    assert read_events()[-1].payload["session_id"] == "ctx-1"


def test_non_string_disagreeing_value_is_overridden(ctx_dir: Path) -> None:
    result = _record({**_PAYLOAD_BASE, "session_id": 5})
    assert result.exit_code == 0
    assert "disagrees" in result.stderr
    assert read_events()[-1].payload["session_id"] == "ctx-1"


def test_agreeing_session_id_has_no_warning(ctx_dir: Path) -> None:
    result = _record({**_PAYLOAD_BASE, "session_id": "ctx-1"})
    assert result.exit_code == 0
    assert result.stderr == ""


def test_stale_cw_session_env_is_ignored(
    ctx_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CW_SESSION", "stale-shared")
    result = _record(dict(_PAYLOAD_BASE))
    assert result.exit_code == 0
    assert read_events()[-1].payload["session_id"] == "ctx-1"


def test_stage_errored_is_derived(ctx_dir: Path) -> None:
    result = _record(dict(_PAYLOAD_BASE), event="stage.errored")
    assert result.exit_code == 0
    assert read_events()[-1].payload["session_id"] == "ctx-1"


def test_other_event_types_are_untouched(ctx_dir: Path) -> None:
    result = CliRunner().invoke(
        main,
        [
            "event",
            "record",
            "session.needs_attention",
            "--payload",
            json.dumps({"session_id": "s1"}),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert read_events()[-1].payload["session_id"] == "s1"


def test_no_context_leaves_payload_byte_identical(
    tmp_events_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cw.cli._hook_io.find_cw_context", lambda _start: None)
    result = _record(dict(_PAYLOAD_BASE))
    assert result.exit_code == 0
    assert result.stderr == _NO_SESSION_WARNING
    assert "session_id" not in read_events()[-1].payload


@pytest.mark.parametrize("value", [None, "", 7])
def test_no_context_and_no_usable_payload_session_id_warns(
    tmp_events_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: object,
) -> None:
    """#3314: an unusable payload session_id with no context also warns."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cw.cli._hook_io.find_cw_context", lambda _start: None)
    result = _record({**_PAYLOAD_BASE, "session_id": value})
    assert result.exit_code == 0
    assert result.stderr == _NO_SESSION_WARNING
    assert read_events()[-1].payload["session_id"] == value


def test_no_context_with_payload_session_id_records_silently(
    tmp_events_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3314: a payload that supplies its own session_id needs no warning."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cw.cli._hook_io.find_cw_context", lambda _start: None)
    result = _record({**_PAYLOAD_BASE, "session_id": "own-1"})
    assert result.exit_code == 0
    assert result.stderr == ""
    assert read_events()[-1].payload["session_id"] == "own-1"


@pytest.mark.parametrize(
    "content",
    [
        {"ticket_id": "173"},
        {"session_id": 7, "ticket_id": "173"},
        None,  # malformed JSON, written below
    ],
)
def test_unusable_context_leaves_payload_unchanged(
    tmp_events_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: dict[str, object] | None,
) -> None:
    path = tmp_path / ".claude" / "cw-context.json"
    path.parent.mkdir(parents=True)
    if content is None:
        path.write_text("{not json", encoding="utf-8")
    else:
        path.write_text(json.dumps(content), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = _record(dict(_PAYLOAD_BASE))
    assert result.exit_code == 0
    assert result.stderr == _NO_SESSION_WARNING
    assert "session_id" not in read_events()[-1].payload


def test_non_utf8_context_fails_open(
    tmp_events_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".claude" / "cw-context.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff")
    monkeypatch.chdir(tmp_path)
    result = _record(dict(_PAYLOAD_BASE))
    assert result.exit_code == 0
    assert result.stderr == _NO_SESSION_WARNING
    assert "session_id" not in read_events()[-1].payload


def test_subdirectory_finds_parent_context(
    ctx_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = ctx_dir / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    result = _record(dict(_PAYLOAD_BASE))
    assert result.exit_code == 0
    assert read_events()[-1].payload["session_id"] == "ctx-1"


def test_bogus_stage_still_rejected(ctx_dir: Path) -> None:
    result = _record({"ticket_id": "173", "stage": "bogus"})
    assert result.exit_code != 0
    assert read_events() == []


def test_stage_event_set_drift_guard() -> None:
    assert frozenset(queues_mod._CONTEXT_SESSION_EVENT_TYPES) == _STAGE_EVENT_TYPES


# --- #3312: the context's ticket_id gates the session_id derivation ----------


@pytest.fixture
def ticket_ctx_dir(
    tmp_path: Path, tmp_events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A cwd whose nearest cw-context carries ticket ``173`` and ``ctx-1``."""
    _write_session_context(tmp_path, headless=True, ticket_id="173", session_id="ctx-1")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize("supplied", [None, "X"])
def test_matching_ticket_derives_as_before(
    ticket_ctx_dir: Path, supplied: str | None
) -> None:
    payload: dict[str, object] = dict(_PAYLOAD_BASE)
    if supplied is not None:
        payload["session_id"] = supplied
    result = _record(payload)
    assert result.exit_code == 0
    assert "differs" not in result.stderr
    assert read_events()[-1].payload["session_id"] == "ctx-1"


def test_mismatching_ticket_is_recorded_as_given_with_warning(
    ticket_ctx_dir: Path,
) -> None:
    result = _record({**_PAYLOAD_BASE, "ticket_id": "999", "session_id": "own-9"})
    assert result.exit_code == 0
    assert result.stderr == (
        "warning: payload ticket_id '999' differs from cw-context.json ticket_id"
        " '173'; not deriving session_id from that context\n"
    )
    assert read_events()[-1].payload["session_id"] == "own-9"


def test_mismatch_without_payload_session_id_warns_once(ticket_ctx_dir: Path) -> None:
    result = _record({**_PAYLOAD_BASE, "ticket_id": "999"})
    assert result.exit_code == 0
    assert result.stderr.count("warning:") == 1
    assert "differs from cw-context.json" in result.stderr
    assert "session_id" not in read_events()[-1].payload


def test_context_without_ticket_still_replaces(ctx_dir: Path) -> None:
    result = _record({**_PAYLOAD_BASE, "ticket_id": "999", "session_id": "X"})
    assert result.exit_code == 0
    assert "differs" not in result.stderr
    assert read_events()[-1].payload["session_id"] == "ctx-1"


@pytest.mark.parametrize("ticket", [None, ""])
def test_payload_without_ticket_still_derives(
    ticket_ctx_dir: Path, ticket: str | None
) -> None:
    payload: dict[str, object] = {"stage": "s2_impl_started"}
    if ticket is not None:
        payload["ticket_id"] = ticket
    result = _record(payload)
    assert result.exit_code == 0
    assert result.stderr == ""
    assert read_events()[-1].payload["session_id"] == "ctx-1"


@pytest.mark.parametrize(("ticket", "matches"), [(173, True), (999, False)])
def test_non_string_payload_ticket_is_compared_via_str(
    ticket_ctx_dir: Path, ticket: int, matches: bool
) -> None:
    result = _record({**_PAYLOAD_BASE, "ticket_id": ticket})
    assert result.exit_code == 0
    assert ("differs" in result.stderr) is not matches
    payload = read_events()[-1].payload
    assert (payload.get("session_id") == "ctx-1") is matches


@pytest.mark.parametrize("ticket", ["#173", " 173 ", "173"])
def test_canonical_forms_of_the_context_ticket_match(
    ticket_ctx_dir: Path, ticket: str
) -> None:
    """The fences' ``$TICKET`` (``jq -r '.ticket_id'`` of the context) and its
    ``#``-prefixed form never trip the guard on a healthy worker."""
    result = _record({**_PAYLOAD_BASE, "ticket_id": ticket})
    assert result.exit_code == 0
    assert result.stderr == ""
    assert read_events()[-1].payload["session_id"] == "ctx-1"


def test_fence_payload_built_from_the_context_never_trips(
    ticket_ctx_dir: Path,
) -> None:
    """Build the payload the way the auto-dev fences do: ``$TICKET`` is the
    context's own ``ticket_id`` read back with ``jq -r '.ticket_id'``."""
    context = json.loads(
        (ticket_ctx_dir / ".claude" / "cw-context.json").read_text(encoding="utf-8")
    )
    ticket = str(context["ticket_id"])
    for form in (ticket, f"#{ticket}"):
        payload = {
            "ticket_id": form,
            "stage": "s2_impl_started",
            "prev_stage": "s1_plan_reviewed",
            "started_at": "2026-11-12T00:00:00Z",
        }
        result = _record(payload)
        assert result.exit_code == 0
        assert result.stderr == ""
        assert read_events()[-1].payload["session_id"] == "ctx-1"
