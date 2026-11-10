"""CLI tests for ``cw dev-queue record-fix-handoff`` (#2651)."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from cw.cli import main
from cw.cli.dev_queue.fix_handoff import record_fix_handoff_command
from cw.dev_queue.storage import _lock as _real_lock
from cw.dev_queue.storage import load_dev_queue
from cw.events import read_events
from cw.models import QueueItemStatus
from tests._cli_review_helpers import _write_session_context
from tests._ticket_view_helpers import make_task, seed_queue


def _run(cwd: Path, prompt: Path, cycle: str = "2") -> Result:
    """Invoke the command from *cwd*; the caller has already chdir'd there."""
    return CliRunner().invoke(
        main,
        [
            "dev-queue",
            "record-fix-handoff",
            "--cycle",
            cycle,
            "--prompt-file",
            str(prompt),
        ],
        catch_exceptions=False,
    )


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _write_session_context(
        tmp_path,
        headless=True,
        client="acme",
        ticket_id="ACM-1",
        session_id="sess-1",
    )
    monkeypatch.chdir(tmp_path)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("# fix\n", encoding="utf-8")
    return prompt


def test_happy_path_records_handoff_on_running_row(
    ctx: Path, tmp_events_dir: Path
) -> None:
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id="sess-1")])
    result = _run(ctx.parent, ctx)
    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == (
        "Recorded fix-agent handoff for ticket 'ACM-1' (acme): cycle 2,"
        " session sess-1.\n"
    )
    pending = load_dev_queue().tasks[0].pending_fix_dispatch
    assert pending is not None
    assert pending.requested_by_session_id == "sess-1"
    assert pending.label == "fix-ACM-1"
    assert pending.cycle == 2
    assert pending.prompt == "# fix\n"
    assert pending.requested_at.tzinfo is not None
    assert read_events() == []


def test_subdirectory_cwd_finds_parent_context(
    ctx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id="sess-1")])
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert _run(nested, ctx).exit_code == 0
    pending = load_dev_queue().tasks[0].pending_fix_dispatch
    assert pending is not None
    assert pending.requested_by_session_id == "sess-1"


def test_stale_cw_session_env_is_ignored(
    ctx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id="sess-1")])
    monkeypatch.setenv("CW_SESSION", "stale-shared")
    assert _run(ctx.parent, ctx).exit_code == 0
    pending = load_dev_queue().tasks[0].pending_fix_dispatch
    assert pending is not None
    assert pending.requested_by_session_id == "sess-1"


def test_row_session_mismatch_warns_and_records(ctx: Path) -> None:
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id="other")])
    result = _run(ctx.parent, ctx)
    assert result.exit_code == 0
    assert (
        "warning: row session_id 'other' != cw-context session_id 'sess-1';"
        " fix_dispatch's unresolved-ref park will skip this row"
    ) in result.stderr
    assert load_dev_queue().tasks[0].pending_fix_dispatch is not None


def test_unstamped_row_has_no_warning(ctx: Path) -> None:
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id=None)])
    result = _run(ctx.parent, ctx)
    assert result.exit_code == 0
    assert "warning" not in result.stderr


def test_pending_row_is_refused(ctx: Path) -> None:
    seed_queue([make_task(status=QueueItemStatus.PENDING, session_id="sess-1")])
    before = load_dev_queue().model_dump_json()
    result = _run(ctx.parent, ctx)
    assert result.exit_code == 1
    assert "not running" in result.output
    assert load_dev_queue().model_dump_json() == before


def test_no_matching_row_is_refused(ctx: Path) -> None:
    seed_queue([])
    before = load_dev_queue().model_dump_json()
    assert _run(ctx.parent, ctx).exit_code == 1
    assert load_dev_queue().model_dump_json() == before


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("session_id", ""),
        ("ticket_id", ""),
        ("client", ""),
        ("session_id", 7),
    ],
)
def test_context_missing_key_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, bad: object
) -> None:
    values: dict[str, object] = {
        "session_id": "sess-1",
        "ticket_id": "ACM-1",
        "client": "acme",
    }
    values[key] = bad
    ctx_path = tmp_path / ".claude" / "cw-context.json"
    ctx_path.parent.mkdir(parents=True)
    ctx_path.write_text(json.dumps({"headless": True, **values}), encoding="utf-8")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id="sess-1")])
    before = load_dev_queue().model_dump_json()
    result = _run(tmp_path, prompt)
    assert result.exit_code == 1
    assert f"has no non-empty '{key}'" in result.output
    assert load_dev_queue().model_dump_json() == before


def test_no_context_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "cw.cli.dev_queue.fix_handoff.find_cw_context_failopen", lambda _p: None
    )
    result = _run(tmp_path, prompt)
    assert result.exit_code == 1
    assert "no .claude/cw-context.json found" in result.output


@pytest.mark.parametrize("body", [b"", b"   \n", b"\xff\xfe"])
def test_bad_prompt_file_is_refused(ctx: Path, body: bytes) -> None:
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id="sess-1")])
    ctx.write_bytes(body)
    before = load_dev_queue().model_dump_json()
    result = _run(ctx.parent, ctx)
    assert result.exit_code == 1
    assert load_dev_queue().model_dump_json() == before


def test_missing_prompt_file_is_refused(ctx: Path) -> None:
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id="sess-1")])
    before = load_dev_queue().model_dump_json()
    result = _run(ctx.parent, ctx.parent / "absent.md")
    assert result.exit_code == 1
    assert "cannot read prompt file" in result.output
    assert load_dev_queue().model_dump_json() == before


@pytest.mark.parametrize("cycle", ["0", None])
def test_bad_cycle_is_usage_error(ctx: Path, cycle: str | None) -> None:
    args = ["dev-queue", "record-fix-handoff", "--prompt-file", str(ctx)]
    if cycle is not None:
        args += ["--cycle", cycle]
    assert CliRunner().invoke(main, args).exit_code == 2


def test_write_failure_is_reported(ctx: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id="sess-1")])

    def boom(_store: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("cw.dev_queue.fix_handoff.save_dev_queue", boom)
    result = _run(ctx.parent, ctx)
    assert result.exit_code == 1
    assert "writing the dev-queue failed" in result.output


def test_lock_is_bounded(ctx: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []

    def spy(*, bounded: bool = False) -> AbstractContextManager[None]:
        seen.append(bounded)
        return _real_lock(bounded=bounded)

    monkeypatch.setattr("cw.dev_queue.fix_handoff._lock", spy)
    seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id="sess-1")])
    assert _run(ctx.parent, ctx).exit_code == 0
    assert seen == [True]


def test_no_client_option() -> None:
    opts = [o for p in record_fix_handoff_command.params for o in p.opts]
    assert "--client" not in opts
