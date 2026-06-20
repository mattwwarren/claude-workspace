"""Tests for cw.cli.session_inspect commands."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from click.testing import CliRunner
from freezegun import freeze_time

if TYPE_CHECKING:
    import pytest

from cw.cli import main
from cw.cli.session_inspect import (
    _SESSION_WAIT_EXIT_HARD_TIMEOUT,
    _SESSION_WAIT_EXIT_TIMED_OUT,
)
from cw.config import load_state, save_state
from cw.models import (
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)

_EXPECTED_SESSION_FIELDS = {
    "id",
    "name",
    "client",
    "purpose",
    "status",
    "origin",
    "started_at",
    "completed_at",
    "completed_reason",
    "idle_at",
    "worktree_path",
    "branch",
    "surface_ref",
    "claude_session_id",
    "lane",
    "last_result",
    "cost_usd",
}


def _make_session(
    tmp_path: Path,
    *,
    session_id: str = "abcd1234",
    name: str = "test-client/impl",
    client: str = "test-client",
    purpose: SessionPurpose = SessionPurpose.IMPL,
    status: SessionStatus = SessionStatus.ACTIVE,
    origin: SessionOrigin = SessionOrigin.USER,
    worktree_path: Path | None = None,
    branch: str | None = None,
    surface_ref: str | None = None,
    claude_session_id: str | None = None,
    last_result: dict[str, object] | None = None,
    cost_usd: float | None = None,
    started_at: datetime | None = None,
) -> Session:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return Session(
        id=session_id,
        name=name,
        client=client,
        purpose=purpose,
        status=status,
        origin=origin,
        workspace_path=workspace,
        worktree_path=worktree_path,
        branch=branch,
        surface_ref=surface_ref,
        claude_session_id=claude_session_id,
        last_result=last_result,
        cost_usd=cost_usd,
        started_at=started_at or datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
    )


def _seed(session: Session) -> None:
    state = load_state()
    state.sessions.append(session)
    save_state(state)


class TestSessionShow:
    def test_show_by_id_prefix_json(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        session = _make_session(tmp_path, session_id="abcd1234")
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "show", "abcd", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert set(data.keys()) == _EXPECTED_SESSION_FIELDS
        assert data["id"] == "abcd1234"
        assert data["client"] == "test-client"

    def test_show_by_claude_session_id_prefix(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        session = _make_session(
            tmp_path,
            session_id="sess0001",
            claude_session_id="uuid-1234-5678-abcd",
        )
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "show", "uuid-1234", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == "sess0001"
        assert data["claude_session_id"] == "uuid-1234-5678-abcd"

    def test_show_not_found_exits_nonzero(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["session", "show", "noexist", "--json"])
        assert result.exit_code != 0
        assert result.exit_code == 1

    @freeze_time("2025-06-01 14:00:00")
    def test_show_human_output_uses_relative_time(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        session = _make_session(
            tmp_path,
            started_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
        )
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "show", "abcd1234"])
        assert result.exit_code == 0
        assert "started_at: 2h ago" in result.output

    def test_show_last_result_none(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        session = _make_session(tmp_path, last_result=None)
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "show", "abcd1234", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["last_result"] is None

    def test_show_worktree_path_serialized_as_str(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        wt = tmp_path / "worktrees" / "my-wt"
        wt.mkdir(parents=True)
        session = _make_session(tmp_path, worktree_path=wt)
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "show", "abcd1234", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data["worktree_path"], str)
        assert data["worktree_path"] == str(wt)


class TestSessionList:
    def test_list_excludes_completed_by_default(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        active = _make_session(
            tmp_path, session_id="sess0001", status=SessionStatus.ACTIVE
        )
        completed = _make_session(
            tmp_path, session_id="sess0002", status=SessionStatus.COMPLETED
        )
        _seed(active)
        _seed(completed)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "list", "--json"])
        assert result.exit_code == 0
        sessions = json.loads(result.output)
        ids = {s["id"] for s in sessions}
        assert "sess0001" in ids
        assert "sess0002" not in ids

    def test_list_excludes_timed_out_by_default(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        active = _make_session(
            tmp_path, session_id="sess0001", status=SessionStatus.ACTIVE
        )
        timed_out = _make_session(
            tmp_path, session_id="sess0002", status=SessionStatus.TIMED_OUT
        )
        _seed(active)
        _seed(timed_out)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "list", "--json"])
        assert result.exit_code == 0
        sessions = json.loads(result.output)
        ids = {s["id"] for s in sessions}
        assert "sess0001" in ids
        assert "sess0002" not in ids

    def test_list_includes_completed_when_status_filter(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        active = _make_session(
            tmp_path, session_id="sess0001", status=SessionStatus.ACTIVE
        )
        completed = _make_session(
            tmp_path, session_id="sess0002", status=SessionStatus.COMPLETED
        )
        _seed(active)
        _seed(completed)
        runner = CliRunner()
        result = runner.invoke(
            main, ["session", "list", "--status", "completed", "--json"]
        )
        assert result.exit_code == 0
        sessions = json.loads(result.output)
        ids = {s["id"] for s in sessions}
        assert "sess0001" not in ids
        assert "sess0002" in ids

    def test_list_filter_by_client(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        s1 = _make_session(tmp_path, session_id="sess0001", client="foo-client")
        s2 = _make_session(tmp_path, session_id="sess0002", client="bar-client")
        _seed(s1)
        _seed(s2)
        runner = CliRunner()
        result = runner.invoke(
            main, ["session", "list", "--client", "foo-client", "--json"]
        )
        assert result.exit_code == 0
        sessions = json.loads(result.output)
        ids = {s["id"] for s in sessions}
        assert "sess0001" in ids
        assert "sess0002" not in ids

    def test_list_filter_by_purpose(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        impl = _make_session(
            tmp_path, session_id="sess0001", purpose=SessionPurpose.IMPL, name="c/impl"
        )
        idea = _make_session(
            tmp_path, session_id="sess0002", purpose=SessionPurpose.IDEA, name="c/idea"
        )
        _seed(impl)
        _seed(idea)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "list", "--purpose", "impl", "--json"])
        assert result.exit_code == 0
        sessions = json.loads(result.output)
        ids = {s["id"] for s in sessions}
        assert "sess0001" in ids
        assert "sess0002" not in ids

    def test_list_filter_by_ticket(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        # ticket_id_for_session parses "client/auto-dev/238" -> "238"
        auto_dev = _make_session(
            tmp_path,
            session_id="sess0001",
            name="claude-workspace/auto-dev/238",
            client="claude-workspace",
        )
        other = _make_session(
            tmp_path,
            session_id="sess0002",
            name="claude-workspace/auto-dev/999",
            client="claude-workspace",
        )
        _seed(auto_dev)
        _seed(other)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "list", "--ticket", "238", "--json"])
        assert result.exit_code == 0
        sessions = json.loads(result.output)
        ids = {s["id"] for s in sessions}
        assert "sess0001" in ids
        assert "sess0002" not in ids

    def test_list_json_is_array(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        s1 = _make_session(tmp_path, session_id="sess0001")
        _seed(s1)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert set(data[0].keys()) == _EXPECTED_SESSION_FIELDS

    def test_list_human_output_shows_headers(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed(_make_session(tmp_path))
        runner = CliRunner()
        result = runner.invoke(main, ["session", "list"])
        assert result.exit_code == 0
        assert "CLIENT" in result.output
        assert "STATUS" in result.output
        assert "ID" in result.output


class TestSessionWait:
    def test_wait_exits_0_when_already_completed(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        session = _make_session(
            tmp_path, session_id="sess0001", status=SessionStatus.COMPLETED
        )
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "wait", "sess0001"])
        assert result.exit_code == 0

    def test_wait_exits_1_when_already_timed_out(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        session = _make_session(
            tmp_path, session_id="sess0001", status=SessionStatus.TIMED_OUT
        )
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "wait", "sess0001"])
        assert result.exit_code == _SESSION_WAIT_EXIT_TIMED_OUT

    def test_wait_timeout_exits_124(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ACTIVE session — will never reach the --until set within the tiny timeout.
        session = _make_session(
            tmp_path, session_id="sess0001", status=SessionStatus.ACTIVE
        )
        _seed(session)
        monkeypatch.setattr("cw.cli.session_inspect._WAIT_POLL_INTERVAL", 0)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "wait", "sess0001", "--timeout", "-1"])
        assert result.exit_code == _SESSION_WAIT_EXIT_HARD_TIMEOUT

    def test_wait_json_output_schema(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        session = _make_session(
            tmp_path, session_id="sess0001", status=SessionStatus.COMPLETED
        )
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "wait", "sess0001", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "session_id" in data
        assert "status" in data
        assert "elapsed_seconds" in data
        assert isinstance(data["elapsed_seconds"], float)

    def test_wait_custom_until_set(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        # --until timed_out: when status is TIMED_OUT, exit 1 per operator resolution
        session = _make_session(
            tmp_path, session_id="sess0001", status=SessionStatus.TIMED_OUT
        )
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(
            main, ["session", "wait", "sess0001", "--until", "timed_out"]
        )
        assert result.exit_code == _SESSION_WAIT_EXIT_TIMED_OUT

    def test_wait_human_output_prints_status(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        session = _make_session(
            tmp_path, session_id="sess0001", status=SessionStatus.COMPLETED
        )
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "wait", "sess0001"])
        assert result.exit_code == 0
        assert "completed" in result.output


class TestSessionResult:
    def test_result_prints_last_result_json(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path, last_result={"status": "shipped", "pr": 42})
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "result", "abcd1234"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "shipped"
        assert data["pr"] == 42

    def test_result_none_exits_1_with_stderr(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path, last_result=None)
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "result", "abcd1234"])
        assert result.exit_code == 1
        assert "No result recorded" in result.output

    def test_result_not_found_exits_nonzero(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["session", "result", "noexist"])
        assert result.exit_code != 0
