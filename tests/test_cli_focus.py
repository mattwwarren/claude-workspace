"""CLI tests for the ``cw focus`` command group (#1644).

``focus set``/``clear``/``show`` are operator-facing commands and are NOT
subject to R3's never-fail contract (only ``cw statusline render`` is), so a
missing session id or an unknown client/lane must surface a clear error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml
from click.testing import CliRunner

from cw.cli import main
from cw.config import clients_file
from cw.models import OrchestratorEventType

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.conftest import CapturedEvent

_SESSION = "sess-focus-1"


def _write_clients(tmp_path: Path) -> None:
    """Write a two-client clients.yaml; ``client-a`` declares two lanes."""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    clients_file().write_text(
        yaml.safe_dump(
            {
                "clients": {
                    "client-a": {
                        "workspace_path": str(workspace),
                        "lanes": [{"name": "impl"}, {"name": "debt"}],
                    },
                    "client-b": {"workspace_path": str(workspace)},
                }
            }
        )
    )


@pytest.fixture
def clients(tmp_config_dir: Path) -> None:
    _write_clients(tmp_config_dir)


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(main, list(args))


class TestFocusSetShowClear:
    def test_set_client_lane_then_show(self, clients: None) -> None:
        result = _invoke("focus", "set", "client-a/impl", "--session", _SESSION)
        assert result.exit_code == 0, result.output
        assert result.output == f"Focus for session '{_SESSION}': client-a/impl\n"

        shown = _invoke("focus", "show", "--session", _SESSION)
        assert shown.exit_code == 0, shown.output
        assert shown.output == f"Focus for session '{_SESSION}': client-a/impl\n"

    def test_set_client_only_omits_lane(self, clients: None) -> None:
        result = _invoke("focus", "set", "client-b", "--session", _SESSION)
        assert result.exit_code == 0, result.output
        assert result.output == f"Focus for session '{_SESSION}': client-b\n"

        shown = _invoke("focus", "show", "--session", _SESSION)
        assert shown.output == f"Focus for session '{_SESSION}': client-b\n"

    def test_show_with_no_prior_focus(self, clients: None) -> None:
        shown = _invoke("focus", "show", "--session", "never-focused")
        assert shown.exit_code == 0, shown.output
        assert shown.output == "No focus set for session 'never-focused'.\n"

    def test_clear_then_show_reports_none(self, clients: None) -> None:
        _invoke("focus", "set", "client-a/impl", "--session", _SESSION)

        cleared = _invoke("focus", "clear", "--session", _SESSION)
        assert cleared.exit_code == 0, cleared.output
        assert cleared.output == f"Cleared focus for session '{_SESSION}'.\n"

        shown = _invoke("focus", "show", "--session", _SESSION)
        assert shown.output == f"No focus set for session '{_SESSION}'.\n"


class TestSessionIdResolution:
    def test_session_resolves_from_env(
        self, clients: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-session")

        result = _invoke("focus", "set", "client-a/debt")
        assert result.exit_code == 0, result.output
        assert result.output == "Focus for session 'env-session': client-a/debt\n"

        shown = _invoke("focus", "show")
        assert shown.output == "Focus for session 'env-session': client-a/debt\n"

    def test_missing_session_id_is_an_error(
        self, clients: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        result = _invoke("focus", "set", "client-a")
        assert result.exit_code != 0
        assert "CLAUDE_CODE_SESSION_ID" in result.output

    def test_missing_session_id_on_show_is_an_error(
        self, clients: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        result = _invoke("focus", "show")
        assert result.exit_code != 0
        assert "CLAUDE_CODE_SESSION_ID" in result.output

    def test_missing_session_id_on_clear_is_an_error(
        self, clients: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        result = _invoke("focus", "clear")
        assert result.exit_code != 0
        assert "CLAUDE_CODE_SESSION_ID" in result.output


class TestFocusSetValidation:
    def test_unknown_client_rejected(self, clients: None) -> None:
        result = _invoke("focus", "set", "nope/impl", "--session", _SESSION)
        assert result.exit_code != 0
        assert "Unknown client 'nope'" in result.output

    def test_undeclared_lane_rejected(self, clients: None) -> None:
        result = _invoke("focus", "set", "client-a/nope", "--session", _SESSION)
        assert result.exit_code != 0
        assert "Lane 'nope' is not declared for client 'client-a'." in result.output


class TestFocusEvents:
    def test_set_emits_focus_set_once(
        self,
        clients: None,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        captured = capture_events("cw.cli.focus")

        result = _invoke("focus", "set", "client-a/impl", "--session", _SESSION)
        assert result.exit_code == 0, result.output

        assert len(captured) == 1
        event_type, payload, _corr = captured[0]
        assert event_type is OrchestratorEventType.FOCUS_SET
        assert payload == {
            "session_id": _SESSION,
            "client": "client-a",
            "lane": "impl",
        }

    def test_set_client_only_emits_null_lane(
        self,
        clients: None,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        captured = capture_events("cw.cli.focus")

        _invoke("focus", "set", "client-b", "--session", _SESSION)

        assert len(captured) == 1
        _etype, payload, _corr = captured[0]
        assert payload == {
            "session_id": _SESSION,
            "client": "client-b",
            "lane": None,
        }

    def test_clear_emits_focus_cleared_with_prior_entry(
        self,
        clients: None,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        _invoke("focus", "set", "client-a/impl", "--session", _SESSION)
        captured = capture_events("cw.cli.focus")

        result = _invoke("focus", "clear", "--session", _SESSION)
        assert result.exit_code == 0, result.output

        assert len(captured) == 1
        event_type, payload, _corr = captured[0]
        assert event_type is OrchestratorEventType.FOCUS_CLEARED
        assert payload == {
            "session_id": _SESSION,
            "client": "client-a",
            "lane": "impl",
        }

    def test_clear_without_prior_focus_still_emits_with_nulls(
        self,
        clients: None,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        captured = capture_events("cw.cli.focus")

        _invoke("focus", "clear", "--session", "never-focused")

        assert len(captured) == 1
        event_type, payload, _corr = captured[0]
        assert event_type is OrchestratorEventType.FOCUS_CLEARED
        assert payload == {
            "session_id": "never-focused",
            "client": None,
            "lane": None,
        }

    def test_show_emits_no_event(
        self,
        clients: None,
        capture_events: Callable[..., list[CapturedEvent]],
    ) -> None:
        _invoke("focus", "set", "client-a/impl", "--session", _SESSION)
        captured = capture_events("cw.cli.focus")

        _invoke("focus", "show", "--session", _SESSION)

        assert captured == []
