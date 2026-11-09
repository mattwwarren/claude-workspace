"""Where review-debt filing finds the cw session and its artifact (#3113)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.cli.review._debt_session import (
    DEFAULT_ARTIFACT,
    default_artifact,
    ensure_default_artifact,
    resolve_artifact,
    session_context,
    session_root,
)
from cw.cli.review.debt import Identity, resolve_identity
from cw.models import HOOK_CONTEXT_RELATIVE_PATH
from cw.review_adjudication import Adjudication
from cw.review_debt_filing import read_artifact
from tests._cli_review_helpers import _session_repo, _write_session_context

if TYPE_CHECKING:
    from collections.abc import Callable

_TICKET = "3113"


def _adjudication(summary: str, outcome: str = "defer") -> Adjudication:
    return Adjudication(
        severity="SHOULD_FIX",
        file="src/thing.py",
        summary=summary,
        outcome=outcome,
        rationale="out of scope",
    )


@pytest.fixture
def plain_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A non-git cwd: the git ceiling makes ``git_toplevel()`` return None."""
    path = tmp_path / "plain"
    path.mkdir()
    monkeypatch.chdir(path)
    return path


class TestSessionRoot:
    def test_a_context_naming_a_ticket_is_a_session(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _session_repo(make_git_repo, monkeypatch, ticket_id=_TICKET)
        assert session_root() == repo.resolve()

    @pytest.mark.parametrize("ticket", ["", 7, None])
    def test_an_empty_or_non_string_ticket_is_not_a_session(
        self, plain_dir: Path, ticket: object
    ) -> None:
        path = plain_dir / HOOK_CONTEXT_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"ticket_id": ticket}), encoding="utf-8")
        assert session_root() is None

    def test_malformed_json_is_not_a_session(self, plain_dir: Path) -> None:
        path = plain_dir / HOOK_CONTEXT_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        assert session_root() is None
        assert session_context() is None

    def test_a_missing_context_is_not_a_session(self, plain_dir: Path) -> None:
        assert session_root() is None
        assert default_artifact() is None

    def test_a_non_git_cwd_falls_back_to_the_cwd(self, plain_dir: Path) -> None:
        _write_session_context(plain_dir, headless=True, ticket_id=_TICKET)
        assert session_root() == plain_dir.resolve()

    def test_a_subdirectory_finds_the_toplevel_context(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _session_repo(make_git_repo, monkeypatch, ticket_id=_TICKET)
        monkeypatch.chdir(repo / "src")
        assert session_root() == repo.resolve()
        assert default_artifact() == repo.resolve() / DEFAULT_ARTIFACT


class TestResolveArtifact:
    def test_an_explicit_path_wins(self, plain_dir: Path) -> None:
        explicit = plain_dir / "elsewhere.md"
        assert resolve_artifact(explicit) == explicit

    def test_the_session_default(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _session_repo(make_git_repo, monkeypatch, ticket_id=_TICKET)
        assert resolve_artifact(None) == repo.resolve() / DEFAULT_ARTIFACT

    def test_none_outside_a_session(self, plain_dir: Path) -> None:
        assert resolve_artifact(None) is None


class TestEnsureDefaultArtifact:
    def test_creates_the_artifact(self, tmp_path: Path) -> None:
        path = tmp_path / ".cw" / "deferred-findings.md"
        ensure_default_artifact(path, [_adjudication("one")])
        assert [a.summary for a in read_artifact(path)] == ["one"]

    def test_merges_the_prior_content(self, tmp_path: Path) -> None:
        path = tmp_path / ".cw" / "deferred-findings.md"
        ensure_default_artifact(path, [_adjudication("one")])
        ensure_default_artifact(path, [_adjudication("two")])
        assert sorted(a.summary for a in read_artifact(path)) == ["one", "two"]

    def test_nothing_deferred_leaves_an_existing_file_alone(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "deferred-findings.md"
        path.write_text("hand written\n", encoding="utf-8")
        ensure_default_artifact(path, [_adjudication("r", outcome="reject")])
        assert path.read_text(encoding="utf-8") == "hand written\n"

    def test_nothing_to_render_writes_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / ".cw" / "deferred-findings.md"
        ensure_default_artifact(path, [])
        assert not path.exists()


def test_resolve_identity_reads_the_toplevel_context_from_a_subdirectory(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _session_repo(make_git_repo, monkeypatch, client="acme", ticket_id=_TICKET)
    monkeypatch.chdir(repo / "src")
    assert resolve_identity(None, None) == Identity("acme", _TICKET)
