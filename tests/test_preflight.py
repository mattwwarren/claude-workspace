"""Tests for the cw-smoke-test preflight helper's tracker-awareness.

preflight.py is a standalone skill script (not part of the cw package), so it
is loaded by path. Only the tracker-resolution + tracker-branching behavior is
covered here — the gh/cw subprocess plumbing is exercised by the smoke test
itself.
"""

from __future__ import annotations

import importlib.util
import json
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


def _make_doctor_json(
    *,
    ok: bool = True,
    clean: bool = True,
    checks: list[dict[str, object]] | None = None,
) -> str:
    return json.dumps(
        {
            "version": 1,
            "ok": ok,
            "clean": clean,
            "checks": checks or [],
            "wedge_findings": [],
        }
    )


class TestCheckCwDoctor:
    """Unit tests for _check_cw_doctor — exercises the JSON-based detection logic."""

    def test_cw_not_on_path_both_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pf = _load()
        monkeypatch.setattr(pf.shutil, "which", lambda _name: None)
        hard, soft = pf._check_cw_doctor()
        assert hard["name"] == "cw_backend_healthy"
        assert hard["passed"] is False
        assert soft["name"] == "cw_doctor_clean"
        assert soft["passed"] is False

    def test_subprocess_uses_json_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pf = _load()
        captured: dict[str, list[str]] = {}

        class _Proc:
            returncode = 0
            stdout = _make_doctor_json()
            stderr = ""

        def _fake_run(argv: list[str], **_k: object) -> _Proc:
            captured["argv"] = argv
            return _Proc()

        monkeypatch.setattr(pf.shutil, "which", lambda _name: "/usr/bin/cw")
        monkeypatch.setattr(pf.subprocess, "run", _fake_run)
        pf._check_cw_doctor()
        assert "--json" in captured["argv"]

    def test_ok_true_clean_true_both_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pf = _load()

        class _Proc:
            returncode = 0
            stdout = _make_doctor_json(ok=True, clean=True)
            stderr = ""

        monkeypatch.setattr(pf.shutil, "which", lambda _name: "/usr/bin/cw")
        monkeypatch.setattr(pf.subprocess, "run", lambda *_a, **_k: _Proc())
        hard, soft = pf._check_cw_doctor()
        assert hard["passed"] is True
        assert soft["passed"] is True

    def test_advisory_warnings_do_not_block_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ok=True but clean=False (warnings) → backend healthy, soft warns."""
        pf = _load()
        warned_checks = [
            {
                "name": "timed_out-merged/abc123",
                "ok": True,
                "warn": True,
                "detail": "session timed out but PR merged",
            }
        ]

        class _Proc:
            returncode = 0
            stdout = _make_doctor_json(ok=True, clean=False, checks=warned_checks)
            stderr = ""

        monkeypatch.setattr(pf.shutil, "which", lambda _name: "/usr/bin/cw")
        monkeypatch.setattr(pf.subprocess, "run", lambda *_a, **_k: _Proc())
        hard, soft = pf._check_cw_doctor()
        assert hard["passed"] is True, "advisory warnings must not block backend check"
        assert soft["passed"] is False, "soft check must reflect warnings"

    def test_backend_core_failure_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """sessions.json ok=False → cw_backend_healthy fails."""
        pf = _load()
        failing_checks = [
            {
                "name": "sessions.json",
                "ok": False,
                "warn": False,
                "detail": "load failed: JSONDecodeError",
            }
        ]

        class _Proc:
            returncode = 1
            stdout = _make_doctor_json(ok=False, clean=False, checks=failing_checks)
            stderr = ""

        monkeypatch.setattr(pf.shutil, "which", lambda _name: "/usr/bin/cw")
        monkeypatch.setattr(pf.subprocess, "run", lambda *_a, **_k: _Proc())
        hard, soft = pf._check_cw_doctor()
        assert hard["passed"] is False
        assert soft["passed"] is False

    def test_project_config_failure_does_not_block_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: project-config failing must not trip cw_backend_healthy.

        This is the #717 false-negative: cw doctor exits non-zero because a
        project-config check failed; the old keyword check tripped on 'config'
        in the output, reporting the backend as unhealthy when it was fine.
        """
        pf = _load()
        checks = [
            {
                "name": "clients.yaml",
                "ok": True,
                "warn": False,
                "detail": "/path/to/clients.yaml",
            },
            {
                "name": "sessions.json",
                "ok": True,
                "warn": False,
                "detail": "/path/to/sessions.json",
            },
            {
                "name": "dev_queue.json",
                "ok": True,
                "warn": False,
                "detail": "parseable",
            },
            {
                "name": "claude-version",
                "ok": True,
                "warn": False,
                "detail": "2.1.139",
            },
            {
                "name": "project-config/claude-workspace",
                "ok": False,
                "warn": False,
                "detail": "tracking.primary.system='foo' is not recognized",
            },
        ]

        class _Proc:
            returncode = 1  # doctor exits non-zero because project-config failed
            stdout = _make_doctor_json(ok=False, clean=False, checks=checks)
            stderr = ""

        monkeypatch.setattr(pf.shutil, "which", lambda _name: "/usr/bin/cw")
        monkeypatch.setattr(pf.subprocess, "run", lambda *_a, **_k: _Proc())
        hard, soft = pf._check_cw_doctor()
        assert hard["passed"] is True, (
            "project-config failure must not flag backend as unhealthy"
        )
        assert soft["passed"] is False, (
            "soft check must still reflect overall not-clean"
        )

    def test_json_parse_failure_nonzero_exit_is_unhealthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If cw doctor --json output is unparseable and exit non-zero → unhealthy."""
        pf = _load()

        class _Proc:
            returncode = 1
            stdout = "fatal error: something crashed"
            stderr = ""

        monkeypatch.setattr(pf.shutil, "which", lambda _name: "/usr/bin/cw")
        monkeypatch.setattr(pf.subprocess, "run", lambda *_a, **_k: _Proc())
        hard, _soft = pf._check_cw_doctor()
        assert hard["passed"] is False

    def test_json_parse_failure_zero_exit_is_healthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If cw doctor --json output is unparseable but exits 0 → healthy."""
        pf = _load()

        class _Proc:
            returncode = 0
            stdout = "not json"
            stderr = ""

        monkeypatch.setattr(pf.shutil, "which", lambda _name: "/usr/bin/cw")
        monkeypatch.setattr(pf.subprocess, "run", lambda *_a, **_k: _Proc())
        hard, _soft = pf._check_cw_doctor()
        assert hard["passed"] is True
