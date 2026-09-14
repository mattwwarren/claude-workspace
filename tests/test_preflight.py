"""Tests for the cw-smoke-test preflight helper's tracker-awareness.

preflight.py is a standalone skill script (not part of the cw package), so it
is loaded by path. Only the tracker-resolution + tracker-branching behavior is
covered here — the gh/cw subprocess plumbing is exercised by the smoke test
itself.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.conftest import init_repo_with_remote

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


def _write_clients_yaml(
    tmp_config_dir: Path,
    name: str,
    *,
    workspace_path: Path | None = None,
    repo_path: Path | None = None,
    branch: str | None = None,
) -> None:
    """Write a minimal clients.yaml entry for *name* under tmp_config_dir.

    File-local copy — mirrors the inline pattern at
    tests/test_doctor.py:1409-1479 (six-plus siblings of this helper already
    exist with no shared utility; extraction is tracked separately as debt
    in #2165, out of scope here). Worktree-mode clients need both repo_path
    and branch set (ClientConfig's validator, src/cw/models/client.py).
    """
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    lines = ["clients:", f"  {name}:"]
    if workspace_path is not None:
        lines.append(f"    workspace_path: {workspace_path}")
    if repo_path is not None:
        assert branch is not None, "repo_path requires branch (ClientConfig validator)"
        lines.append(f"    repo_path: {repo_path}")
        lines.append(f"    branch: {branch}")
    (config_dir / "clients.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_agent_repo(root: Path, *, tracker: str | None = "github-issues") -> Path:
    """Build a repo root with the required plan-review agents present locally."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "plan-reviewer.md").write_text("stub\n", encoding="utf-8")
    (agents / "plan-soundness-reviewer.md").write_text("stub\n", encoding="utf-8")
    if tracker is not None:
        _write_config(root, tracker)
    return root


def _install_check_stubs(
    pf: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, tuple[str, ...]],
) -> None:
    """Stub the four subprocess-touching checks so main() runs with no real gh/cw."""

    def _fake_doctor() -> tuple[dict[str, object], dict[str, object]]:
        return (
            {
                "name": "cw_backend_healthy",
                "passed": True,
                "severity": "hard",
                "detail": "",
            },
            {
                "name": "cw_doctor_clean",
                "passed": True,
                "severity": "soft",
                "detail": "",
            },
        )

    def _fake_ticket_open(ticket: str, repo: str, tracker: str) -> dict[str, object]:
        captured["ticket_open"] = (ticket, repo, tracker)
        return {"name": "ticket_open", "passed": True, "severity": "hard", "detail": ""}

    def _fake_no_open_pr(ticket: str, repo: str, tracker: str) -> dict[str, object]:
        captured["no_open_pr"] = (ticket, repo, tracker)
        return {
            "name": "no_open_pr_for_ticket",
            "passed": True,
            "severity": "hard",
            "detail": "",
        }

    def _fake_not_queued(ticket: str, client: str) -> dict[str, object]:
        captured["not_queued"] = (ticket, client)
        return {
            "name": "not_already_queued",
            "passed": True,
            "severity": "hard",
            "detail": "",
        }

    monkeypatch.setattr(pf, "_check_cw_doctor", _fake_doctor)
    monkeypatch.setattr(pf, "_check_ticket_open", _fake_ticket_open)
    monkeypatch.setattr(pf, "_check_no_open_pr", _fake_no_open_pr)
    monkeypatch.setattr(pf, "_check_not_queued", _fake_not_queued)


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

    def test_json_parse_failure_any_exit_is_unhealthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If cw doctor --json output is unparseable → always hard fail (any rc).

        Malformed stdout means doctor itself is broken regardless of exit code.
        """
        pf = _load()

        class _Proc:
            returncode = 0
            stdout = "not json"
            stderr = ""

        monkeypatch.setattr(pf.shutil, "which", lambda _name: "/usr/bin/cw")
        monkeypatch.setattr(pf.subprocess, "run", lambda *_a, **_k: _Proc())
        hard, _soft = pf._check_cw_doctor()
        assert hard["passed"] is False
        assert "unparseable" in hard["detail"]

    def test_stale_cw_build_rc2_degrades_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rc=2 (--json not supported by older cw) → degraded pass, not a hard block.

        Click returns rc=2 with 'Error: No such option: --json' when the flag
        is unrecognized. The backend may still be healthy; do not block the
        smoke test on a build-version mismatch.
        """
        pf = _load()

        class _Proc:
            returncode = 2
            stdout = ""
            stderr = "Error: No such option: --json"

        monkeypatch.setattr(pf.shutil, "which", lambda _name: "/usr/bin/cw")
        monkeypatch.setattr(pf.subprocess, "run", lambda *_a, **_k: _Proc())
        hard, soft = pf._check_cw_doctor()
        assert hard["passed"] is True, "stale build must not hard-block backend check"
        assert "stale cw build" in hard["detail"]
        assert soft["passed"] is False

    def test_daemon_reachable_failure_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """daemon-reachable ok=False → cw_backend_healthy fails.

        Note: current doctor.py uses warn=True (not ok=False) for daemon health;
        this test uses a synthetic payload to verify allowlist membership.
        """
        pf = _load()
        failing_checks = [
            {
                "name": "daemon-reachable",
                "ok": False,
                "warn": False,
                "detail": "no response from daemon",
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


class TestResolveClientRepoRoot:
    """_resolve_client_repo_root maps --client to a filesystem repo root (#2158)."""

    def test_resolves_workspace_path_client(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        pf = _load()
        client_root = tmp_path / "acme"
        client_root.mkdir()
        _write_clients_yaml(tmp_config_dir, "acme", workspace_path=client_root)
        assert pf._resolve_client_repo_root("acme") == client_root

    def test_resolves_repo_path_over_workspace_path(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        pf = _load()
        repo_root = tmp_path / "acme-repo"
        repo_root.mkdir()
        _write_clients_yaml(
            tmp_config_dir, "acme", repo_path=repo_root, branch="dev/123"
        )
        assert pf._resolve_client_repo_root("acme") == repo_root

    def test_dangling_client_raises(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        pf = _load()
        other_root = tmp_path / "other"
        other_root.mkdir()
        _write_clients_yaml(tmp_config_dir, "other-client", workspace_path=other_root)
        with pytest.raises(pf._ClientRepoUnresolvedError) as exc_info:
            pf._resolve_client_repo_root("missing-client")
        assert "missing-client" in str(exc_info.value)
        assert "clients.yaml" in str(exc_info.value)

    def test_no_clients_yaml_falls_back_to_resolve_repo_root(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pf = _load()
        sentinel = Path("/sentinel/repo/root")
        monkeypatch.setattr(pf, "_resolve_repo_root", lambda: sentinel)
        assert pf._resolve_client_repo_root("anything") == sentinel

    def test_malformed_yaml_raises_client_repo_unresolved(
        self, tmp_config_dir: Path
    ) -> None:
        """Not-even-valid-YAML clients.yaml must not crash preflight (#2158)."""
        pf = _load()
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n  acme:\n    workspace_path: [unterminated\n", encoding="utf-8"
        )
        with pytest.raises(pf._ClientRepoUnresolvedError) as exc_info:
            pf._resolve_client_repo_root("acme")
        assert "clients.yaml" in str(exc_info.value)

    def test_schema_invalid_yaml_raises_client_repo_unresolved(
        self, tmp_config_dir: Path
    ) -> None:
        """Valid YAML failing ClientConfig validation must not crash (#2158)."""
        pf = _load()
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n  acme:\n    workspace_path: /tmp/acme\n"
            "    not_a_real_field: true\n",
            encoding="utf-8",
        )
        with pytest.raises(pf._ClientRepoUnresolvedError) as exc_info:
            pf._resolve_client_repo_root("acme")
        assert "clients.yaml" in str(exc_info.value)


class TestResolveRepoSlugWrapper:
    """_resolve_repo_slug thin-wraps cw.pr_hydrate's slug resolver."""

    def test_delegates_to_cw_pr_hydrate(self, tmp_path: Path) -> None:
        pf = _load()
        repo = init_repo_with_remote(
            tmp_path / "widgets", "git@github.com:acme/widgets.git"
        )
        assert pf._resolve_repo_slug(repo) == "acme/widgets"

    def test_none_when_unresolvable(self, tmp_path: Path) -> None:
        pf = _load()
        repo = init_repo_with_remote(tmp_path / "no-remote", None)
        assert pf._resolve_repo_slug(repo) is None


class TestMainRepoResolution:
    """main()'s end-to-end wiring of --client-derived repo/tracker (#2158)."""

    def test_client_repo_root_used_for_agents_and_tracker_not_script_location(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pf = _load()
        client_root = tmp_path / "client-root"
        _make_agent_repo(client_root, tracker="github-issues")
        decoy_root = tmp_path / "decoy-root"
        decoy_root.mkdir()

        _write_clients_yaml(tmp_config_dir, "acme", workspace_path=client_root)
        monkeypatch.setattr(pf, "_resolve_repo_root", lambda: decoy_root)
        monkeypatch.setattr(pf, "_resolve_repo_slug", lambda _root: "acme/widgets")

        captured: dict[str, tuple[str, ...]] = {}
        _install_check_stubs(pf, monkeypatch, captured)
        monkeypatch.setattr(
            sys, "argv", ["preflight.py", "--ticket-id", "1", "--client", "acme"]
        )

        rc = pf.main()
        report = json.loads(capsys.readouterr().out)

        assert rc == 0
        agents_check = next(
            c for c in report["checks"] if c["name"] == "agents_present"
        )
        assert agents_check["passed"] is True
        assert captured["ticket_open"][2] == "github-issues"

    def test_repo_flag_overrides_client_derived_slug(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pf = _load()
        client_root = tmp_path / "client-root"
        client_root.mkdir()
        monkeypatch.setattr(
            pf, "_resolve_client_repo_root", lambda _client: client_root
        )
        monkeypatch.setattr(pf, "_resolve_repo_slug", lambda _root: "derived/repo")

        captured: dict[str, tuple[str, ...]] = {}
        _install_check_stubs(pf, monkeypatch, captured)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "preflight.py",
                "--ticket-id",
                "1",
                "--client",
                "acme",
                "--repo",
                "explicit/repo",
            ],
        )

        pf.main()
        capsys.readouterr()

        assert captured["ticket_open"][1] == "explicit/repo"
        assert captured["no_open_pr"][1] == "explicit/repo"

    def test_client_derives_repo_when_repo_omitted(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pf = _load()
        client_root = tmp_path / "client-root"
        client_root.mkdir()
        monkeypatch.setattr(
            pf, "_resolve_client_repo_root", lambda _client: client_root
        )
        monkeypatch.setattr(pf, "_resolve_repo_slug", lambda _root: "derived/repo")

        captured: dict[str, tuple[str, ...]] = {}
        _install_check_stubs(pf, monkeypatch, captured)
        monkeypatch.setattr(
            sys, "argv", ["preflight.py", "--ticket-id", "1", "--client", "acme"]
        )

        pf.main()
        capsys.readouterr()

        assert captured["ticket_open"][1] == "derived/repo"
        assert captured["no_open_pr"][1] == "derived/repo"

    def test_dangling_client_exits_1_with_single_check(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pf = _load()

        def _raise_unresolved(_client: str) -> Path:
            msg = "boom"
            raise pf._ClientRepoUnresolvedError(msg)

        def _must_not_call(*_a: object, **_k: object) -> object:
            msg = "must not be called when client repo root is unresolved"
            raise AssertionError(msg)

        monkeypatch.setattr(pf, "_resolve_client_repo_root", _raise_unresolved)
        monkeypatch.setattr(pf, "_check_agents", _must_not_call)
        monkeypatch.setattr(pf, "_check_cw_doctor", _must_not_call)
        monkeypatch.setattr(pf, "_check_ticket_open", _must_not_call)
        monkeypatch.setattr(pf, "_check_no_open_pr", _must_not_call)
        monkeypatch.setattr(pf, "_check_not_queued", _must_not_call)
        monkeypatch.setattr(
            sys, "argv", ["preflight.py", "--ticket-id", "1", "--client", "acme"]
        )

        rc = pf.main()
        report = json.loads(capsys.readouterr().out)

        assert rc == 1
        assert report["ok"] is False
        assert report["checks"] == [
            {
                "name": "client_repo_resolved",
                "passed": False,
                "severity": "hard",
                "detail": "boom",
            }
        ]

    def test_unresolvable_repo_slug_without_override_hard_fails_and_skips_gh_checks(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pf = _load()
        client_root = tmp_path / "client-root"
        client_root.mkdir()
        monkeypatch.setattr(
            pf, "_resolve_client_repo_root", lambda _client: client_root
        )
        monkeypatch.setattr(pf, "_resolve_repo_slug", lambda _root: None)

        def _must_not_call(*_a: object, **_k: object) -> object:
            msg = "must not be called when repo slug is unresolved"
            raise AssertionError(msg)

        monkeypatch.setattr(pf, "_check_ticket_open", _must_not_call)
        monkeypatch.setattr(pf, "_check_no_open_pr", _must_not_call)
        monkeypatch.setattr(
            pf,
            "_check_cw_doctor",
            lambda: (
                {
                    "name": "cw_backend_healthy",
                    "passed": True,
                    "severity": "hard",
                    "detail": "",
                },
                {
                    "name": "cw_doctor_clean",
                    "passed": True,
                    "severity": "soft",
                    "detail": "",
                },
            ),
        )
        monkeypatch.setattr(
            pf,
            "_check_not_queued",
            lambda _t, _c: {
                "name": "not_already_queued",
                "passed": True,
                "severity": "hard",
                "detail": "",
            },
        )
        monkeypatch.setattr(
            sys, "argv", ["preflight.py", "--ticket-id", "1", "--client", "acme"]
        )

        rc = pf.main()
        report = json.loads(capsys.readouterr().out)

        assert rc == 1
        repo_check = next(c for c in report["checks"] if c["name"] == "repo_resolved")
        assert repo_check["passed"] is False
        assert repo_check["severity"] == "hard"

    def test_malformed_clients_yaml_hard_fails_with_structured_json(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A malformed clients.yaml must not crash main() (#2158)."""
        pf = _load()
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n  acme:\n    workspace_path: [unterminated\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            sys, "argv", ["preflight.py", "--ticket-id", "1", "--client", "acme"]
        )

        rc = pf.main()
        report = json.loads(capsys.readouterr().out)

        assert rc == 1
        assert report["ok"] is False
        assert len(report["checks"]) == 1
        check = report["checks"][0]
        assert check["name"] == "client_repo_resolved"
        assert check["passed"] is False
        assert check["severity"] == "hard"
        assert "clients.yaml" in check["detail"]

    def test_schema_invalid_clients_yaml_hard_fails_with_structured_json(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A schema-invalid clients.yaml must not crash main() (#2158)."""
        pf = _load()
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n  acme:\n    workspace_path: /tmp/acme\n"
            "    not_a_real_field: true\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            sys, "argv", ["preflight.py", "--ticket-id", "1", "--client", "acme"]
        )

        rc = pf.main()
        report = json.loads(capsys.readouterr().out)

        assert rc == 1
        assert report["ok"] is False
        assert len(report["checks"]) == 1
        check = report["checks"][0]
        assert check["name"] == "client_repo_resolved"
        assert check["passed"] is False
        assert check["severity"] == "hard"
        assert "clients.yaml" in check["detail"]

    def test_structurally_malformed_clients_yaml_hard_fails_with_structured_json(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A 'clients:' key whose value is a list, not a mapping, is a
        ConfigValidationError from load_clients() (#2158's root-cause fix in
        cw.config) and must not crash main() (operator round-3 resolution,
        #2158)."""
        pf = _load()
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text(
            "clients:\n  - acme\n  - beta\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            sys, "argv", ["preflight.py", "--ticket-id", "1", "--client", "acme"]
        )

        rc = pf.main()
        report = json.loads(capsys.readouterr().out)

        assert rc == 1
        assert report["ok"] is False
        assert len(report["checks"]) == 1
        check = report["checks"][0]
        assert check["name"] == "client_repo_resolved"
        assert check["passed"] is False
        assert check["severity"] == "hard"
        assert "clients.yaml" in check["detail"]

    def test_unreadable_clients_yaml_hard_fails_with_structured_json(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An unreadable clients.yaml (OSError from read_text) must not crash
        main() (round-2 operator resolution, #2158). clients.yaml is a
        directory, so Path.read_text() raises a genuine IsADirectoryError
        (an OSError subclass) with no monkeypatching (round-3 operator
        resolution, #2158)."""
        pf = _load()
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").mkdir()
        monkeypatch.setattr(
            sys, "argv", ["preflight.py", "--ticket-id", "1", "--client", "acme"]
        )

        rc = pf.main()
        report = json.loads(capsys.readouterr().out)

        assert rc == 1
        assert report["ok"] is False
        assert len(report["checks"]) == 1
        check = report["checks"][0]
        assert check["name"] == "client_repo_resolved"
        assert check["passed"] is False
        assert check["severity"] == "hard"
        assert "clients.yaml" in check["detail"]

    def test_non_utf8_clients_yaml_hard_fails_with_structured_json(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A non-UTF-8 clients.yaml (UnicodeDecodeError from read_text) must
        not crash main() (round-2 operator resolution, #2158). Writing real
        non-UTF-8 bytes makes Path.read_text()'s default UTF-8 decode raise a
        genuine UnicodeDecodeError with no monkeypatching (round-3 operator
        resolution, #2158)."""
        pf = _load()
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_bytes(b"\xff\xfe\x00\x01")
        monkeypatch.setattr(
            sys, "argv", ["preflight.py", "--ticket-id", "1", "--client", "acme"]
        )

        rc = pf.main()
        report = json.loads(capsys.readouterr().out)

        assert rc == 1
        assert report["ok"] is False
        assert len(report["checks"]) == 1
        check = report["checks"][0]
        assert check["name"] == "client_repo_resolved"
        assert check["passed"] is False
        assert check["severity"] == "hard"
        assert "clients.yaml" in check["detail"]

    def test_empty_clients_yaml_hard_fails_without_fallback(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An existing, empty clients.yaml with --client X must fail loudly,
        never silently fall back to _resolve_repo_root() (round-2 operator
        resolution, #2158)."""
        pf = _load()
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").write_text("", encoding="utf-8")

        def _fail_if_called() -> Path:
            msg = "_resolve_repo_root() must not be called for a populated file"
            raise AssertionError(msg)

        monkeypatch.setattr(pf, "_resolve_repo_root", _fail_if_called)
        monkeypatch.setattr(
            sys, "argv", ["preflight.py", "--ticket-id", "1", "--client", "acme"]
        )

        rc = pf.main()
        report = json.loads(capsys.readouterr().out)

        assert rc == 1
        assert report["ok"] is False
        assert len(report["checks"]) == 1
        check = report["checks"][0]
        assert check["name"] == "client_repo_resolved"
        assert check["passed"] is False
        assert check["severity"] == "hard"
        assert "clients.yaml" in check["detail"]
        assert "acme" in check["detail"]
