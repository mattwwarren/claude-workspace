"""Tests for cw.cli.session_inspect commands."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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
    _resolve_session,
)
from cw.config import load_state, save_state
from cw.models import (
    CompletionReason,
    LastResultSource,
    ReapReason,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
)
from cw.session_retention import prune_sessions
from tests.conftest import _make_daemon_session, _make_diff


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
    **overrides: object,
) -> Session:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, object] = {
        "id": session_id,
        "name": name,
        "client": client,
        "purpose": purpose,
        "status": status,
        "origin": origin,
        "workspace_path": workspace,
        "worktree_path": worktree_path,
        "branch": branch,
        "surface_ref": surface_ref,
        "claude_session_id": claude_session_id,
        "last_result": last_result,
        "cost_usd": cost_usd,
        "started_at": started_at or datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
    }
    kwargs.update(overrides)
    return _make_daemon_session(**kwargs)


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
        assert set(data.keys()) == set(Session.model_fields.keys())
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

    def test_show_json_field_drift_guard(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Guards against session_inspect's hand-listed dict silently dropping
        fields as Session grows (GitHub #1624)."""
        session = _make_session(tmp_path)
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "show", "abcd1234", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        missing = set(Session.model_fields.keys()) - set(data.keys())
        assert not missing, f"session show --json is missing fields: {sorted(missing)}"

    def test_show_json_includes_previously_missing_fields(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        session = _make_session(
            tmp_path,
            stage=Stage.REVIEW,
            last_result_source=LastResultSource.STOP_HOOK_HARVEST,
            reap_reason=ReapReason.IDLE_STALL,
            parent_session_id="parent1",
        )
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "show", "abcd1234", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["stage"] == "review"
        assert data["last_result_source"] == "stop_hook_harvest"
        assert data["reap_reason"] == "idle_stall"
        assert data["parent_session_id"] == "parent1"

    def test_show_json_datetime_fields_use_z_suffix(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """model_dump(mode="json") emits an RFC3339 "Z" suffix instead of the
        prior hand-rolled .isoformat()'s "+00:00" — an intentional, disclosed
        breaking change for session show/list --json (GitHub #1624), matching
        PR #1620's precedent for TicketTask's created_at."""
        session = _make_session(
            tmp_path,
            started_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
            completed_at=datetime(2025, 6, 1, 13, 0, 0, tzinfo=UTC),
            idle_at=datetime(2025, 6, 1, 12, 30, 0, tzinfo=UTC),
        )
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "show", "abcd1234", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        for field in ("started_at", "completed_at", "idle_at"):
            assert data[field].endswith("Z"), data[field]
            assert "+00:00" not in data[field], data[field]

    def test_show_json_enum_and_path_representations_unchanged(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Regression lock: purpose/status/origin/completed_reason keep
        emitting the bare .value string under model_dump(mode="json") — same
        representation as the pre-#1624 hand-listed dict. worktree_path's
        str(...) representation is covered separately by
        test_show_worktree_path_serialized_as_str above."""
        session = _make_session(
            tmp_path,
            purpose=SessionPurpose.DEBT,
            status=SessionStatus.COMPLETED,
            origin=SessionOrigin.DAEMON,
            completed_reason=CompletionReason.NORMAL,
        )
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "show", "abcd1234", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["purpose"] == "debt"
        assert data["status"] == "completed"
        assert data["origin"] == "daemon"
        assert data["completed_reason"] == "normal"

    def test_show_human_output_surfaces_operator_relevant_fields(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """_print_session_human independently hand-lists fields (GitHub
        #1624) — guard that stage/last_result_source/reap_reason are
        rendered with their values, not just present as labels."""
        session = _make_session(
            tmp_path,
            stage=Stage.IMPL,
            last_result_source=LastResultSource.GIT_SYNTHESIS,
            reap_reason=ReapReason.WALL_CLOCK_BUDGET,
        )
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "show", "abcd1234"])
        assert result.exit_code == 0
        assert "stage: impl" in result.output
        assert "last_result_source: git_synthesis" in result.output
        assert "reap_reason: wall_clock_budget" in result.output


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
        assert set(data[0].keys()) == set(Session.model_fields.keys())

    def test_list_json_field_drift_guard(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Guards against session_inspect's hand-listed dict silently
        dropping fields as Session grows (GitHub #1624)."""
        session = _make_session(tmp_path)
        _seed(session)
        runner = CliRunner()
        result = runner.invoke(main, ["session", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        missing = set(Session.model_fields.keys()) - set(data[0].keys())
        assert not missing, f"session list --json is missing fields: {sorted(missing)}"

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

    def test_result_surfaces_diagnostics_path_in_blocker_details(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A CODEX_REVIEW_UNPARSEABLE blocked last_result carries the diagnostics
        bundle path in blocker.details, which `session result` prints (#1239)."""
        from cw.codex_review import synthesize_codex_review_result
        from cw.executor_diagnostics import diagnostics_bundle_dir
        from cw.models import Stage, TicketTask
        from cw.review_findings import ReviewerRunFailure

        result, _verdict = synthesize_codex_review_result(
            task=TicketTask(ticket_id="T-1", client="c", stage=Stage.REVIEW),
            worktree=tmp_path,
            documents=[],
            failures=[ReviewerRunFailure(role="Code Quality Reviewer", reason="crash")],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="abcd1234",
            default_branch="main",
            fix_loop_enabled=False,
        )
        session = _make_session(tmp_path, last_result=result.model_dump(mode="json"))
        _seed(session)
        runner = CliRunner()
        cli_result = runner.invoke(main, ["session", "result", "abcd1234"])
        assert cli_result.exit_code == 0
        data = json.loads(cli_result.output)
        # tmp_config_dir relocates state_dir() away from the real home, so
        # _render_bundle_path takes its absolute-fallback branch: the
        # rendered pointer is exactly "[diagnostics: <absolute bundle dir>]".
        bundle = diagnostics_bundle_dir("abcd1234")
        assert (
            data["blocker"]["details"]
            == f"Code Quality Reviewer (crash) [diagnostics: {bundle}]"
        )

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


# ---------------------------------------------------------------------------
# #1983: archive-aware session resolution + session list archive notice
# ---------------------------------------------------------------------------


def _seed_and_archive(
    tmp_path: Path,
    *,
    session_id: str,
    prune_at: datetime,
) -> None:
    """Seed one very old COMPLETED session and prune it into a dated archive."""
    session = _make_session(
        tmp_path,
        session_id=session_id,
        name=f"client-a/auto-dev/T-{session_id}",
        status=SessionStatus.COMPLETED,
        started_at=prune_at - timedelta(days=400),
        completed_at=prune_at - timedelta(days=400),
    )
    _seed(session)
    with freeze_time(prune_at):
        prune_sessions()


class TestResolveSessionArchiveFallback:
    def test_resolve_session_falls_back_to_archive_on_hot_miss(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_and_archive(
            tmp_path,
            session_id="arc00001",
            prune_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        )
        assert load_state().sessions == []

        found = _resolve_session("arc00001")
        assert found is not None
        assert found.id == "arc00001"

    def test_resolve_session_scans_newest_archive_first_stops_on_hit(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_and_archive(
            tmp_path,
            session_id="arc00001",
            prune_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        )
        _seed_and_archive(
            tmp_path,
            session_id="arc00002",
            prune_at=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
        )

        found = _resolve_session("arc")
        assert found is not None
        assert found.id == "arc00002"

    def test_resolve_session_returns_none_when_absent_in_hot_and_archives(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_and_archive(
            tmp_path,
            session_id="arc00001",
            prune_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        )
        assert _resolve_session("zzzzzzzz") is None

    def test_session_show_resolves_an_archived_session(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_and_archive(
            tmp_path,
            session_id="arc00001",
            prune_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        )
        runner = CliRunner()
        result = runner.invoke(main, ["session", "show", "arc00001", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["id"] == "arc00001"


class TestSessionListArchiveNotice:
    def test_session_list_status_completed_prints_notice_when_archives_exist(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_and_archive(
            tmp_path,
            session_id="arc00001",
            prune_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        )
        runner = CliRunner()
        result = runner.invoke(main, ["session", "list", "--status", "completed"])
        assert result.exit_code == 0, result.output
        assert "archived session file" in result.output

    def test_session_list_status_completed_no_notice_when_no_archives(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed(_make_session(tmp_path, status=SessionStatus.COMPLETED))
        runner = CliRunner()
        result = runner.invoke(main, ["session", "list", "--status", "completed"])
        assert result.exit_code == 0, result.output
        assert "archived session file" not in result.output

    def test_session_list_default_status_never_prints_notice(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        _seed_and_archive(
            tmp_path,
            session_id="arc00001",
            prune_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        )
        runner = CliRunner()
        result = runner.invoke(main, ["session", "list"])
        assert result.exit_code == 0, result.output
        assert "archived session file" not in result.output
