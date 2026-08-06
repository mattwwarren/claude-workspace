"""CLI tests for ``cw statusline render`` (#1644).

``render`` is machine-invoked on every assistant message, so R3 is a CLI-level
contract: exit 0 always, and an empty segment must print nothing at all (not a
blank line).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml
from click.testing import CliRunner

from cw.cli import main
from cw.config import clients_file, focus_file
from cw.dev_queue import save_dev_queue
from cw.focus import set_focus
from cw.models import DevQueueStore, QueueItemStatus
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

_SESSION = "sess-cli-statusline"


def _write_clients(tmp_path: Path) -> Path:
    ws = tmp_path / "ws" / "client-a"
    ws.mkdir(parents=True, exist_ok=True)
    clients_file().write_text(
        yaml.safe_dump(
            {
                "clients": {
                    "client-a": {
                        "workspace_path": str(ws),
                        "lanes": [{"name": "impl"}],
                    }
                }
            }
        )
    )
    return ws


def _render(*args: str) -> Any:
    return CliRunner().invoke(main, ["statusline", "render", *args])


def test_focused_session_renders_the_expected_line(tmp_config_dir: Path) -> None:
    _write_clients(tmp_config_dir)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="T-1",
                    client="client-a",
                    lane="impl",
                    status=QueueItemStatus.RUNNING,
                ),
            ]
        )
    )
    set_focus(_SESSION, "client-a", "impl")

    result = _render("--session", _SESSION, "--cwd", str(tmp_config_dir))

    assert result.exit_code == 0, result.output
    assert result.output == "client-a/impl 1▶ 0⧗\n"


def test_step_three_prints_nothing(tmp_config_dir: Path) -> None:
    _write_clients(tmp_config_dir)

    result = _render("--session", _SESSION, "--cwd", str(tmp_config_dir / "nowhere"))

    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_unknown_session_and_unmapped_cwd_exits_zero(tmp_config_dir: Path) -> None:
    _write_clients(tmp_config_dir)
    set_focus("a-different-session", "client-a", "impl")

    result = _render("--session", "no-such-session", "--cwd", str(tmp_config_dir))

    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_malformed_focus_json_exits_zero(tmp_config_dir: Path) -> None:
    """R3 at the CLI layer, not just the unit layer."""
    _write_clients(tmp_config_dir)
    focus_file().parent.mkdir(parents=True, exist_ok=True)
    focus_file().write_text("{{{ not json")

    result = _render("--session", _SESSION, "--cwd", str(tmp_config_dir))

    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_session_defaults_to_env_var(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_clients(tmp_config_dir)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-sess")
    save_dev_queue(DevQueueStore(tasks=[]))
    set_focus("env-sess", "client-a")

    result = _render("--cwd", str(tmp_config_dir))

    assert result.exit_code == 0, result.output
    assert result.output == "client-a 0▶ 0⧗\n"


def test_missing_session_env_still_exits_zero(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_clients(tmp_config_dir)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    result = _render("--cwd", str(tmp_config_dir / "nowhere"))

    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_cwd_defaults_to_process_cwd(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _write_clients(tmp_config_dir)
    save_dev_queue(
        DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id="T-1", client="client-a", status=QueueItemStatus.PENDING
                )
            ]
        )
    )
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.chdir(ws)

    result = _render()

    assert result.exit_code == 0, result.output
    assert result.output == "client-a 0▶ 1⧗\n"


def test_unexpected_error_still_exits_zero(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command's own broad except is the R3 guarantee — no @handle_errors."""

    def _boom(*_a: object, **_k: object) -> str:
        msg = "render exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr("cw.cli.statusline.render_work_segment", _boom)

    result = _render("--session", _SESSION, "--cwd", str(tmp_config_dir))

    assert result.exit_code == 0, result.output
    assert result.output == ""
