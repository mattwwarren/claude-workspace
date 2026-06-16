"""Tests for the cw-smoke-test preflight helper's tracker-awareness.

preflight.py is a standalone skill script (not part of the cw package), so it
is loaded by path. Only the tracker-resolution + tracker-branching behavior is
covered here — the gh/cw subprocess plumbing is exercised by the smoke test
itself.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

_PREFLIGHT = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "skills"
    / "cw-smoke-test"
    / "scripts"
    / "preflight.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("preflight_under_test", _PREFLIGHT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_config(root: Path, system: str) -> None:
    cfg = root / ".claude"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "project-config.yaml").write_text(
        f"tracking:\n  primary:\n    system: {system}\n", encoding="utf-8"
    )


class TestResolveTracker:
    def test_reads_github_issues(self, tmp_path: Path) -> None:
        pf = _load()
        _write_config(tmp_path, "github-issues")
        assert pf._resolve_tracker(tmp_path) == "github-issues"

    def test_reads_linear(self, tmp_path: Path) -> None:
        pf = _load()
        _write_config(tmp_path, "linear")
        assert pf._resolve_tracker(tmp_path) == "linear"

    def test_absent_file_defaults_linear_legacy(self, tmp_path: Path) -> None:
        pf = _load()
        assert pf._resolve_tracker(tmp_path) == "linear"


class TestTicketOpenHonorsTracker:
    def test_linear_soft_skips_without_calling_gh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pf = _load()

        def _boom(*_a: object, **_k: object) -> object:
            msg = "gh must not be called for a non-github tracker"
            raise AssertionError(msg)

        monkeypatch.setattr(pf.subprocess, "run", _boom)
        result = pf._check_ticket_open("GEN-403", "owner/repo", "linear")
        assert result["severity"] == "soft"
        assert result["passed"] is True
        assert "linear" in result["detail"].lower()


class TestNoOpenPrHonorsTracker:
    def test_linear_uses_branch_head_not_issue_search(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pf = _load()
        captured: dict[str, list[str]] = {}

        class _Proc:
            returncode = 0
            stdout = "[]"
            stderr = ""

        def _fake_run(argv: list[str], **_k: object) -> _Proc:
            captured["argv"] = argv
            return _Proc()

        monkeypatch.setattr(pf.shutil, "which", lambda _name: "/usr/bin/gh")
        monkeypatch.setattr(pf.subprocess, "run", _fake_run)
        result = pf._check_no_open_pr("GEN-403", "owner/repo", "linear")
        argv = captured["argv"]
        assert "--head" in argv
        assert "auto-dev/GEN-403" in argv
        # branch lookup must not fall back to issue-number free-text search
        assert not any("in:title" in str(a) for a in argv)
        assert result["passed"] is True

    def test_github_issues_still_uses_issue_search(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pf = _load()
        captured: dict[str, list[str]] = {}

        class _Proc:
            returncode = 0
            stdout = "[]"
            stderr = ""

        def _fake_run(argv: list[str], **_k: object) -> _Proc:
            captured["argv"] = argv
            return _Proc()

        monkeypatch.setattr(pf.shutil, "which", lambda _name: "/usr/bin/gh")
        monkeypatch.setattr(pf.subprocess, "run", _fake_run)
        pf._check_no_open_pr("403", "owner/repo", "github-issues")
        assert any("in:title" in str(a) for a in captured["argv"])
