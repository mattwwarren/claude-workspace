"""``cw.cli.dev_queue._tracker_post``: the tracker-posting policy (#2732).

Shared by ``resolve --post`` and ``approve --post-marker``: the GitHub-or-unknown
tracker gate, the ``gh`` working directory and the post-outcome echo. The
dedup, the PLAN-stage guard and the poster itself stay in the two commands and
are covered by their own suites.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from cw.cli.dev_queue._tracker_post import (
    github_post_cwd,
    report_post_outcome,
    tracker_is_github_or_unknown,
)
from cw.models import ClientConfig
from tests._shared.cli_preflight import _write_project_config_yaml

if TYPE_CHECKING:
    from pathlib import Path

_GITHUB_CONFIG = "tracking:\n  primary:\n    system: github-issues\n"
_LINEAR_CONFIG = "tracking:\n  primary:\n    system: linear\n"
_NO_TRACKING_CONFIG = "other: value\n"
_NOTE_KWARGS = {
    "flag": "--post",
    "noun": "relay block",
    "ticket_id": "ACME-7",
    "client": "acme",
    "row_note": "Recorded on the row.",
}


def _client(tmp_path: Path, **overrides: Path) -> ClientConfig:
    return ClientConfig(name="acme", workspace_path=tmp_path / "ws", **overrides)


def _completed(returncode: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode)


class TestTrackerIsGithubOrUnknown:
    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            pytest.param(_GITHUB_CONFIG, True, id="github"),
            pytest.param(None, True, id="no-config-file"),
            pytest.param(_NO_TRACKING_CONFIG, True, id="no-tracking-key"),
            pytest.param(_LINEAR_CONFIG, False, id="linear"),
            pytest.param(
                "tracking:\n  primary:\n    system: jira\n", False, id="other-system"
            ),
        ],
    )
    def test_gate(self, tmp_path: Path, config: str | None, expected: bool) -> None:
        if config is not None:
            _write_project_config_yaml(tmp_path / "ws", config)
        assert tracker_is_github_or_unknown(_client(tmp_path)) is expected


class TestGithubPostCwd:
    def test_github_returns_workspace_and_prints_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_project_config_yaml(tmp_path / "ws", _GITHUB_CONFIG)
        assert github_post_cwd(_client(tmp_path), **_NOTE_KWARGS) == tmp_path / "ws"
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_unresolved_tracker_still_posts(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert github_post_cwd(_client(tmp_path), **_NOTE_KWARGS) == tmp_path / "ws"
        assert capsys.readouterr().out == ""

    def test_repo_path_wins_over_workspace_path(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        client = _client(tmp_path, repo_path=repo)
        assert github_post_cwd(client, **_NOTE_KWARGS) == repo

    def test_known_non_github_tracker_returns_none_and_echoes_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_project_config_yaml(tmp_path / "ws", _LINEAR_CONFIG)
        assert github_post_cwd(_client(tmp_path), **_NOTE_KWARGS) is None
        captured = capsys.readouterr()
        assert captured.out == (
            "--post: tracker is 'linear', not GitHub — the relay block is not"
            " posted to ACME-7 (acme). Recorded on the row.\n"
        )
        assert captured.err == ""

    def test_note_carries_every_caller_supplied_part(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_project_config_yaml(tmp_path / "ws", _LINEAR_CONFIG)
        assert (
            github_post_cwd(
                _client(tmp_path),
                flag="--post-marker",
                noun="marker comment",
                ticket_id="ACME-9",
                client="other",
                row_note="Stamped.",
            )
            is None
        )
        assert capsys.readouterr().out == (
            "--post-marker: tracker is 'linear', not GitHub — the marker comment"
            " is not posted to ACME-9 (other). Stamped.\n"
        )


class TestReportPostOutcome:
    def test_none_result_is_failure_on_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert report_post_outcome(None, posted="ok", failed="bad") is False
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "bad\n"

    def test_nonzero_returncode_is_failure_on_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert report_post_outcome(_completed(1), posted="ok", failed="bad") is False
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "bad\n"

    def test_zero_returncode_is_success_on_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert report_post_outcome(_completed(0), posted="ok", failed="bad") is True
        captured = capsys.readouterr()
        assert captured.out == "ok\n"
        assert captured.err == ""
