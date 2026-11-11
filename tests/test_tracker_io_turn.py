"""Tests for cw.tracker_io._turn: the tracker-IO agent turn end to end (#3057).

Hermetic: every turn runs through ``FakeCodexRunner`` or a local recording
runner, never a real ``codex``. The codex user-layer MCP config is staged
with ``tests/_backend_mcp_helpers.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.codex_review._const import _is_spawn_error
from cw.codex_runner import CodexRunResult, FakeCodexRunner
from cw.config import state_dir
from cw.gh import AGENT_COMMENT_MARKER
from cw.models.client import TrackerMcpGateConfig
from cw.tracker import TRACKER_LINEAR
from cw.tracker_io import (
    TURN_TIMEOUT_SECONDS,
    PublicState,
    TrackerIoContext,
    TrackerIoOutcome,
    TrackerIoReason,
    plan_turn_parts,
    post_parts_via_turn,
)
from tests._backend_mcp_helpers import (
    McpStage,
    codex_tracker_client,
    stage_mcp_config,
)
from tests._tool_fakes import tracker_io_turn_result

if TYPE_CHECKING:
    from cw.codex_runner import CodexRunner

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")

_TICKET = "GEN-7"
_SECRET = "sk-proj-" + "a1B2c3D4" * 4


def _context(
    tmp_path: Path,
    runner: CodexRunner,
    servers: dict[str, str] | None = None,
) -> TrackerIoContext:
    if servers is None:
        servers = {"codex": "linear"}
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    return TrackerIoContext(
        runner=runner, client=codex_tracker_client(tmp_path, servers), worktree=worktree
    )


def _turn(
    context: TrackerIoContext, text: str = "verdict text", tracker: str = "linear"
) -> TrackerIoOutcome:
    return post_parts_via_turn(
        _TICKET, text, tracker=tracker, context=context, session_id="s-1"
    )


def _stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: McpStage = "enabled"
) -> Path:
    return stage_mcp_config(monkeypatch, tmp_path, "codex", state)


def _verified_runner(context: TrackerIoContext, text: str) -> FakeCodexRunner:
    parts = plan_turn_parts(text, context)
    return FakeCodexRunner(output_file_content=tracker_io_turn_result(_TICKET, parts))


def test_plan_turn_parts_unmarked_for_the_github_path(tmp_path: Path) -> None:
    """#3060: marked=False leaves the marker to post_issue_comment."""
    context = _context(tmp_path, FakeCodexRunner())
    marked = plan_turn_parts("plan text", context)
    unmarked = plan_turn_parts("plan text", context, marked=False)
    assert all(AGENT_COMMENT_MARKER in part for part in marked)
    assert not any(AGENT_COMMENT_MARKER in part for part in unmarked)
    assert len(marked) == len(unmarked)


class _RecordingRunner:
    """Reads the real ``-o`` file, as ``RealCodexRunner`` does; can raise."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.raises = raises
        self.cwds: list[Path] = []
        self.argvs: list[list[str]] = []

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        self.cwds.append(worktree)
        self.argvs.append(list(argv))
        assert worktree.is_dir()
        assert (worktree / "schema.json").is_file()
        if self.raises is not None:
            raise self.raises
        out = Path(argv[argv.index("-o") + 1])
        content = out.read_text(encoding="utf-8") if out.exists() else None
        return CodexRunResult(0, "", "", output_file_content=content)


# --- happy path ----------------------------------------------------------------


def test_happy_path_two_parts_redacted_and_marked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)
    placeholder = _context(tmp_path, FakeCodexRunner())
    home_path = f"{Path.home()}/private/notes.txt"
    worktree_path = f"{placeholder.worktree}/src/module.py"
    head = f"See {home_path} and {worktree_path}; token {_SECRET}\n"
    text = head + ("x" * 99 + "\n") * 700
    runner = _verified_runner(placeholder, text)
    context = TrackerIoContext(
        runner=runner, client=placeholder.client, worktree=placeholder.worktree
    )

    outcome = _turn(context, text)

    assert outcome.verified
    assert outcome.public_state is PublicState.CONFIRMED
    assert outcome.parts_total == 2
    assert [p.index for p in outcome.posted] == [1, 2]
    assert outcome.marker_survived is True
    (call,) = runner.calls
    assert call["timeout"] == TURN_TIMEOUT_SECONDS
    cwd = call["cwd"]
    assert isinstance(cwd, Path)
    assert cwd.parent == state_dir() / "codex-review"
    assert not cwd.exists()
    stdin = call["stdin"]
    assert isinstance(stdin, str)
    parts = plan_turn_parts(text, context)
    assert len(parts) == 2
    for part in parts:
        assert part in stdin
        assert part.endswith(AGENT_COMMENT_MARKER)
    assert home_path not in stdin
    assert worktree_path not in stdin
    assert str(placeholder.worktree) not in stdin
    assert _SECRET not in stdin


def test_happy_path_logs_ids_at_info(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _stage(monkeypatch, tmp_path)
    context = _context(tmp_path, FakeCodexRunner())
    context = TrackerIoContext(
        runner=_verified_runner(context, "v"),
        client=context.client,
        worktree=context.worktree,
    )
    with caplog.at_level(logging.INFO, logger="cw.tracker_io._turn"):
        assert _turn(context, "v").verified
    assert any("cmt-1" in r.getMessage() for r in caplog.records)


def test_other_declared_servers_are_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _stage(monkeypatch, tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8") + '\n[mcp_servers.github]\ncommand = "x"\n',
        encoding="utf-8",
    )
    runner = FakeCodexRunner()
    _turn(_context(tmp_path, runner))
    argv = runner.calls[0]["argv"]
    assert isinstance(argv, list)
    assert "mcp_servers.github.enabled=false" in argv
    assert "mcp_servers.linear.enabled=false" not in argv


# --- scratch (R7) --------------------------------------------------------------


def test_each_turn_gets_a_fresh_scratch_dir_that_is_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)
    runner = _RecordingRunner()
    context = _context(tmp_path, runner)
    _turn(context)
    _turn(context)
    first, second = runner.cwds
    assert first != second
    assert not first.exists()
    assert not second.exists()
    for cwd, argv in zip(runner.cwds, runner.argvs, strict=True):
        assert Path(argv[argv.index("-o") + 1]).parent == cwd


def test_stale_result_in_another_dir_is_never_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)
    runner = _RecordingRunner()
    context = _context(tmp_path, runner)
    stale = state_dir() / "codex-review" / "stale-uuid" / "result.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(
        tracker_io_turn_result(_TICKET, plan_turn_parts("verdict text", context)),
        encoding="utf-8",
    )
    outcome = _turn(context)
    assert outcome.reason is TrackerIoReason.RESULT_UNPARSEABLE
    assert outcome.public_state is PublicState.POSSIBLE
    assert stale.exists()


def test_scratch_is_removed_after_a_raising_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)
    runner = _RecordingRunner(raises=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        _turn(_context(tmp_path, runner))
    (cwd,) = runner.cwds
    assert not cwd.exists()


def test_scratch_is_removed_after_a_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)
    runner = FakeCodexRunner(simulate_timeout=True)
    outcome = _turn(_context(tmp_path, runner))
    assert outcome.reason is TrackerIoReason.TURN_TIMEOUT
    assert outcome.public_state is PublicState.POSSIBLE
    assert len(runner.calls) == 1
    cwd = runner.calls[0]["cwd"]
    assert isinstance(cwd, Path)
    assert not cwd.exists()


def test_unwritable_scratch_refuses_before_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)

    def _no_scratch(_name: str) -> Path:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("cw.tracker_io._turn._codex_scratch_dir", _no_scratch)
    runner = FakeCodexRunner()
    outcome = _turn(_context(tmp_path, runner))
    assert outcome.reason is TrackerIoReason.SCRATCH_UNWRITABLE
    assert outcome.public_state is PublicState.NONE
    assert runner.calls == []


def test_unwritable_schema_refuses_before_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)

    def _fail_schema_write(path: Path, *_args: object, **_kwargs: object) -> int:
        if path.name == "schema.json":
            message = "disk full"
            raise OSError(message)
        return 0

    monkeypatch.setattr(Path, "write_text", _fail_schema_write)
    runner = FakeCodexRunner()
    outcome = _turn(_context(tmp_path, runner))
    assert outcome.reason is TrackerIoReason.SCRATCH_UNWRITABLE
    assert outcome.public_state is PublicState.NONE
    assert runner.calls == []


# --- pre-turn checks -----------------------------------------------------------


@pytest.mark.parametrize("state", ["absent", "disabled"])
def test_mcp_not_enabled_never_launches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: McpStage
) -> None:
    _stage(monkeypatch, tmp_path, state)
    runner = FakeCodexRunner()
    outcome = _turn(_context(tmp_path, runner))
    assert outcome.reason is TrackerIoReason.TRACKER_MCP_NOT_ENABLED
    assert outcome.public_state is PublicState.NONE
    assert outcome.posted == ()
    assert runner.calls == []


def test_tracker_mcp_gate_enabled_flag_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)
    runner = FakeCodexRunner()
    client = codex_tracker_client(tmp_path)
    client = client.model_copy(
        update={
            "tracker_mcp_gate": TrackerMcpGateConfig(
                enabled=False, servers={"codex": "linear"}
            )
        }
    )
    context = TrackerIoContext(runner=runner, client=client, worktree=tmp_path)
    outcome = _turn(context)
    assert outcome.reason is TrackerIoReason.RESULT_UNPARSEABLE
    assert outcome.public_state is PublicState.POSSIBLE
    assert len(runner.calls) == 1


def test_unknown_mcp_state_proceeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No user-layer file: UNKNOWN, fail-open (and nothing to disable)."""
    _stage(monkeypatch, tmp_path, "missing")
    runner = FakeCodexRunner()
    _turn(_context(tmp_path, runner))
    assert len(runner.calls) == 1
    argv = runner.calls[0]["argv"]
    assert isinstance(argv, list)
    assert not any(str(a).endswith(".enabled=false") for a in argv)


@pytest.mark.parametrize(
    "servers", [{"opencode": "linear"}, None], ids=["no-codex-entry", "no-gate"]
)
def test_unnamed_server_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, servers: dict[str, str] | None
) -> None:
    _stage(monkeypatch, tmp_path)
    runner = FakeCodexRunner()
    context = TrackerIoContext(
        runner=runner,
        client=codex_tracker_client(tmp_path, servers),
        worktree=tmp_path,
    )
    outcome = _turn(context)
    assert outcome.reason is TrackerIoReason.MCP_SERVER_UNNAMED
    assert outcome.public_state is PublicState.NONE
    assert runner.calls == []


def test_invalid_server_name_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)
    runner = FakeCodexRunner()
    outcome = _turn(_context(tmp_path, runner, {"codex": "lin.ear"}))
    assert outcome.reason is TrackerIoReason.MCP_SERVER_UNNAMED
    assert runner.calls == []


def test_unsupported_tracker_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)
    runner = FakeCodexRunner()
    outcome = _turn(_context(tmp_path, runner), tracker="jira")
    assert outcome.reason is TrackerIoReason.TRACKER_UNSUPPORTED
    assert outcome.public_state is PublicState.NONE
    assert runner.calls == []


def test_invalid_ticket_id_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)
    runner = FakeCodexRunner()
    outcome = post_parts_via_turn(
        "GEN 7",
        "v",
        tracker=TRACKER_LINEAR,
        context=_context(tmp_path, runner),
        session_id="s-1",
    )
    assert outcome.reason is TrackerIoReason.PLAN_REFUSED
    assert runner.calls == []


def test_blank_text_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stage(monkeypatch, tmp_path)
    runner = FakeCodexRunner()
    outcome = _turn(_context(tmp_path, runner), " \n")
    assert outcome.reason is TrackerIoReason.PLAN_REFUSED
    assert runner.calls == []


def test_unplannable_text_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)

    def _refuse(*_args: object, **_kwargs: object) -> None:
        msg = "the comment wrapper alone exceeds the limit"
        raise ValueError(msg)

    monkeypatch.setattr("cw.tracker_io._turn.plan_parts", _refuse)
    runner = FakeCodexRunner()
    outcome = _turn(_context(tmp_path, runner))
    assert outcome.reason is TrackerIoReason.PLAN_REFUSED
    assert runner.calls == []


# --- result cases --------------------------------------------------------------


def test_agent_tool_unavailable_with_zero_parts_is_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)
    runner = FakeCodexRunner(
        output_file_content=tracker_io_turn_result(
            _TICKET, (), status="tool_unavailable", detail="no linear tools"
        )
    )
    outcome = _turn(_context(tmp_path, runner))
    assert outcome.reason is TrackerIoReason.TOOL_UNAVAILABLE
    assert outcome.public_state is PublicState.NONE


def test_nonzero_exit_is_possible_without_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)
    runner = FakeCodexRunner(returncode=1, stderr="config load failed")
    outcome = _turn(_context(tmp_path, runner))
    assert outcome.reason is TrackerIoReason.TURN_EXIT_NONZERO
    assert outcome.public_state is PublicState.POSSIBLE
    assert len(runner.calls) == 1


def test_count_mismatch_is_confirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)
    context = _context(tmp_path, FakeCodexRunner())
    text = ("y" * 99 + "\n") * 700
    parts = plan_turn_parts(text, context)
    runner = FakeCodexRunner(
        output_file_content=tracker_io_turn_result(_TICKET, parts[:1])
    )
    outcome = _turn(
        TrackerIoContext(
            runner=runner, client=context.client, worktree=context.worktree
        ),
        text,
    )
    assert outcome.reason is TrackerIoReason.IDS_COUNT_MISMATCH
    assert outcome.public_state is PublicState.CONFIRMED
    assert [p.comment_id for p in outcome.posted] == ["cmt-1"]
    assert outcome.parts_total == 2


def test_spawn_error_is_codex_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)
    runner = FakeCodexRunner(returncode=127, stderr="codex: command not found")
    assert _is_spawn_error(runner.run(tmp_path, [], None))
    runner.calls.clear()
    outcome = _turn(_context(tmp_path, runner))
    assert outcome.reason is TrackerIoReason.CODEX_UNAVAILABLE
    assert outcome.public_state is PublicState.NONE
    assert len(runner.calls) == 1


def test_runner_oserror_is_codex_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage(monkeypatch, tmp_path)
    runner = _RecordingRunner(raises=PermissionError("not executable"))
    outcome = _turn(_context(tmp_path, runner))
    assert outcome.reason is TrackerIoReason.CODEX_UNAVAILABLE
    assert outcome.public_state is PublicState.NONE
    (cwd,) = runner.cwds
    assert not cwd.exists()
