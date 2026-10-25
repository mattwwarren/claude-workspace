"""Tests for the ``cw body-sha`` command (#2761).

The command prints :func:`cw.plan_fingerprint.compute_body_sha` of a ticket
body -- fetched from GitHub via ``fetch_issue_body`` or read from a file -- so
a plan-stage worker persists exactly the value ``cw dev-queue approve``
recomputes. Every failure exits 1: a worker must never persist a fabricated
hash. ``fetch_issue_body`` is stubbed; client and tracker resolution run for
real.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner, Result

from cw.cli import main
from cw.plan_fingerprint import compute_body_sha
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests.conftest import _make_agent_repo

if TYPE_CHECKING:
    from pathlib import Path

_BODY = "Ticket body.\n\nSecond paragraph."

type FetchCalls = list[tuple[str, int, Path | None]]


def _invoke(*args: str, stdin: bytes | None = None) -> Result:
    return CliRunner().invoke(main, ["body-sha", *args], input=stdin)


def _stub_fetch(monkeypatch: pytest.MonkeyPatch, body: str | None) -> FetchCalls:
    """Stub the command's ``fetch_issue_body`` binding; return its call log."""
    calls: FetchCalls = []

    def _fake(ticket_id: str, timeout: int, *, cwd: Path | None = None) -> str | None:
        calls.append((ticket_id, timeout, cwd))
        return body

    monkeypatch.setattr("cw.cli.body_sha.fetch_issue_body", _fake)
    return calls


@pytest.fixture
def acme_root(tmp_config_dir: Path, tmp_path: Path) -> Path:
    """A registered GitHub-tracked client ``acme``."""
    root = _make_agent_repo(tmp_path / "acme", tracker="github-issues")
    write_clients_yaml(ClientSpec("acme", workspace_path=root))
    return root


@pytest.fixture
def linear_root(tmp_config_dir: Path, tmp_path: Path) -> Path:
    """A registered client ``lin`` whose tracker is positively Linear."""
    root = _make_agent_repo(tmp_path / "lin", tracker="linear")
    write_clients_yaml(ClientSpec("lin", workspace_path=root))
    return root


class TestFetchMode:
    def test_prints_compute_body_sha_of_the_fetched_body(
        self, acme_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_fetch(monkeypatch, _BODY)

        result = _invoke("123", "-c", "acme")

        assert result.exit_code == 0, result.output
        assert result.stdout == compute_body_sha(_BODY) + "\n"
        assert len(calls) == 1
        ticket_id, _timeout, cwd = calls[0]
        assert ticket_id == "123"
        assert cwd == acme_root

    @pytest.mark.parametrize("suffix", ["", "\n", "\r\n", "  \n\n"])
    def test_trailing_whitespace_on_the_body_does_not_change_the_output(
        self, acme_root: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
    ) -> None:
        _stub_fetch(monkeypatch, _BODY + suffix)

        result = _invoke("123", "--client", "acme")

        assert result.exit_code == 0, result.output
        assert result.stdout == compute_body_sha(_BODY) + "\n"

    def test_cwd_prefers_the_client_repo_path(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _make_agent_repo(tmp_path / "ws", tracker="github-issues")
        repo = tmp_path / "repo"
        repo.mkdir()
        write_clients_yaml(ClientSpec("acme", workspace_path=workspace, repo_path=repo))
        calls = _stub_fetch(monkeypatch, _BODY)

        result = _invoke("123", "-c", "acme")

        assert result.exit_code == 0, result.output
        assert calls[0][2] == repo

    def test_output_matches_the_sha_approve_computes(
        self, acme_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Approve hashes the raw fetch through ``compute_body_sha`` too, so
        a persisted value from this command never reads as drift."""
        _stub_fetch(monkeypatch, _BODY + "\n")

        result = _invoke("123", "-c", "acme")

        assert result.stdout.strip() == compute_body_sha(_BODY + "\n")

    def test_leading_hash_is_stripped(
        self, acme_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_fetch(monkeypatch, _BODY)

        result = _invoke("#123", "-c", "acme")

        assert result.exit_code == 0, result.output
        assert calls[0][0] == "123"

    def test_fetch_failure_exits_1_and_prints_no_sha(
        self, acme_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_fetch(monkeypatch, None)

        result = _invoke("123", "-c", "acme")

        assert result.exit_code == 1
        assert result.stdout == ""
        assert "could not fetch" in result.stderr
        assert "123" in result.stderr

    def test_linear_tracker_exits_1_and_points_at_body_file(
        self, linear_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_fetch(monkeypatch, _BODY)

        result = _invoke("ENG-1", "-c", "lin")

        assert result.exit_code == 1
        assert result.stdout == ""
        assert "--body-file" in result.stderr
        assert calls == []

    def test_unknown_client_exits_1(
        self, acme_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_fetch(monkeypatch, _BODY)

        result = _invoke("123", "-c", "nope")

        assert result.exit_code == 1
        assert "Unknown client 'nope'" in result.stderr
        assert calls == []


class TestBodyFileMode:
    def test_body_file_path(self, tmp_path: Path) -> None:
        body_file = tmp_path / "ticket-body.txt"
        body_file.write_bytes((_BODY + "\n").encode("utf-8"))

        result = _invoke("--body-file", str(body_file))

        assert result.exit_code == 0, result.output
        assert result.stdout == compute_body_sha(_BODY) + "\n"

    def test_body_file_stdin(self) -> None:
        result = _invoke("--body-file", "-", stdin=_BODY.encode("utf-8"))

        assert result.exit_code == 0, result.output
        assert result.stdout == compute_body_sha(_BODY) + "\n"

    def test_crlf_bytes_are_not_translated(self, tmp_path: Path) -> None:
        crlf_body = "line one\r\nline two\r\n"
        body_file = tmp_path / "ticket-body.txt"
        body_file.write_bytes(crlf_body.encode("utf-8"))

        result = _invoke("--body-file", str(body_file))

        assert result.exit_code == 0, result.output
        assert result.stdout.strip() == compute_body_sha(crlf_body)
        assert result.stdout.strip() != compute_body_sha("line one\nline two\n")

    def test_body_file_matches_fetch_mode(
        self, acme_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_fetch(monkeypatch, _BODY)
        body_file = tmp_path / "ticket-body.txt"
        body_file.write_bytes((_BODY + "\n").encode("utf-8"))

        fetched = _invoke("123", "-c", "acme")
        from_file = _invoke("--body-file", str(body_file))

        assert fetched.exit_code == from_file.exit_code == 0
        assert fetched.stdout == from_file.stdout

    def test_missing_body_file_exits_1(self, tmp_path: Path) -> None:
        result = _invoke("--body-file", str(tmp_path / "absent.txt"))

        assert result.exit_code == 1
        assert result.stdout == ""
        assert "cannot read --body-file" in result.stderr

    def test_unreadable_body_file_exits_1(self, tmp_path: Path) -> None:
        result = _invoke("--body-file", str(tmp_path))

        assert result.exit_code == 1
        assert "cannot read --body-file" in result.stderr

    def test_non_utf8_body_file_exits_1(self, tmp_path: Path) -> None:
        body_file = tmp_path / "ticket-body.txt"
        body_file.write_bytes(b"caf\xe9\n")

        result = _invoke("--body-file", str(body_file))

        assert result.exit_code == 1
        assert result.stdout == ""
        assert "not valid UTF-8" in result.stderr


class TestInputValidation:
    def test_neither_ticket_nor_body_file_exits_1(self) -> None:
        result = _invoke()

        assert result.exit_code == 1
        assert "TICKET" in result.stderr
        assert "--body-file" in result.stderr

    def test_both_ticket_and_body_file_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_fetch(monkeypatch, _BODY)
        body_file = tmp_path / "ticket-body.txt"
        body_file.write_text(_BODY, encoding="utf-8")

        result = _invoke("123", "-c", "acme", "--body-file", str(body_file))

        assert result.exit_code == 1
        assert "not both" in result.stderr
        assert calls == []

    def test_ticket_without_client_exits_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _stub_fetch(monkeypatch, _BODY)

        result = _invoke("123")

        assert result.exit_code == 1
        assert "--client" in result.stderr
        assert calls == []

    def test_help_names_the_command(self) -> None:
        result = _invoke("--help")

        assert result.exit_code == 0
        assert "cw body-sha" in result.output
        assert "-c, --client" in result.output
        assert "--body-file" in result.output
