"""Tests for ``cw.preflight``, the logic behind ``cw preflight`` (#2697).

Covers tracker resolution and tracker branching, the ``cw doctor`` /
``dev-queue`` check parsing, client and repo-root resolution, and
``run_preflight``'s report wiring. The real gh/cw subprocesses are stubbed;
the CLI surface is covered in ``test_cli_preflight.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import click
import cw.preflight as pf
import pytest
from click.testing import CliRunner

from cw.cli import main
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests.conftest import (
    _install_check_stubs,
    _make_agent_repo,
    _write_tracker_config,
    git_in,
    init_repo_with_remote,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_REPORT_KEYS = frozenset(
    {"ok", "ticket_id", "client", "repo", "repo_root", "tracker", "checks"}
)
_CHECK_KEYS = frozenset({"name", "passed", "severity", "detail"})


class _Proc:
    """A ``subprocess.run`` result stand-in."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestResolveTracker:
    def test_reads_github_issues(self, tmp_path: Path) -> None:
        _write_tracker_config(tmp_path, "github-issues")
        assert pf._resolve_tracker(tmp_path) == "github-issues"

    def test_reads_linear(self, tmp_path: Path) -> None:
        _write_tracker_config(tmp_path, "linear")
        assert pf._resolve_tracker(tmp_path) == "linear"

    def test_absent_file_defaults_linear_legacy(self, tmp_path: Path) -> None:
        assert pf._resolve_tracker(tmp_path) == "linear"


class TestTicketOpenHonorsTracker:
    def test_linear_soft_skips_without_calling_gh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, list[str]] = {}

        def _fake_run(argv: list[str], **_k: object) -> _Proc:
            captured["argv"] = argv
            return _Proc(stdout="[]")

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
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, list[str]] = {}

        def _fake_run(argv: list[str], **_k: object) -> _Proc:
            captured["argv"] = argv
            return _Proc(stdout="[]")

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


def _patch_doctor(monkeypatch: pytest.MonkeyPatch, proc: _Proc) -> None:
    monkeypatch.setattr(pf.shutil, "which", lambda _name: "/usr/bin/cw")
    monkeypatch.setattr(pf.subprocess, "run", lambda *_a, **_k: proc)


class TestCheckCwDoctor:
    """Unit tests for _check_cw_doctor — exercises the JSON-based detection logic."""

    def test_cw_not_on_path_both_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pf.shutil, "which", lambda _name: None)
        hard, soft = pf._check_cw_doctor()
        assert hard["name"] == "cw_backend_healthy"
        assert hard["passed"] is False
        assert soft["name"] == "cw_doctor_clean"
        assert soft["passed"] is False

    def test_subprocess_uses_json_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, list[str]] = {}

        def _fake_run(argv: list[str], **_k: object) -> _Proc:
            captured["argv"] = argv
            return _Proc(stdout=_make_doctor_json())

        monkeypatch.setattr(pf.shutil, "which", lambda _name: "/usr/bin/cw")
        monkeypatch.setattr(pf.subprocess, "run", _fake_run)
        pf._check_cw_doctor()
        assert "--json" in captured["argv"]

    def test_ok_true_clean_true_both_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_doctor(monkeypatch, _Proc(stdout=_make_doctor_json(ok=True, clean=True)))
        hard, soft = pf._check_cw_doctor()
        assert hard["passed"] is True
        assert soft["passed"] is True
        assert soft["detail"] == "no issues"

    def test_advisory_warnings_do_not_block_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ok=True but clean=False (warnings) → backend healthy, soft warns."""
        warned_checks: list[dict[str, object]] = [
            {
                "name": "timed_out-merged/abc123",
                "ok": True,
                "warn": True,
                "detail": "session timed out but PR merged",
            }
        ]
        stdout = _make_doctor_json(ok=True, clean=False, checks=warned_checks)
        _patch_doctor(monkeypatch, _Proc(stdout=stdout))
        hard, soft = pf._check_cw_doctor()
        assert hard["passed"] is True, "advisory warnings must not block backend check"
        assert soft["passed"] is False, "soft check must reflect warnings"
        assert soft["detail"].startswith("cw doctor reported issues:\n")

    def test_backend_core_failure_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """sessions.json ok=False → cw_backend_healthy fails."""
        failing_checks: list[dict[str, object]] = [
            {
                "name": "sessions.json",
                "ok": False,
                "warn": False,
                "detail": "load failed: JSONDecodeError",
            }
        ]
        stdout = _make_doctor_json(ok=False, clean=False, checks=failing_checks)
        _patch_doctor(monkeypatch, _Proc(returncode=1, stdout=stdout))
        hard, soft = pf._check_cw_doctor()
        assert hard["passed"] is False
        assert hard["detail"] == "sessions.json: load failed: JSONDecodeError"
        assert soft["passed"] is False

    def test_project_config_failure_does_not_block_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: project-config failing must not trip cw_backend_healthy.

        This is the #717 false-negative: cw doctor exits non-zero because a
        project-config check failed; the old keyword check tripped on 'config'
        in the output, reporting the backend as unhealthy when it was fine.
        """
        checks: list[dict[str, object]] = [
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
        stdout = _make_doctor_json(ok=False, clean=False, checks=checks)
        # doctor exits non-zero because project-config failed
        _patch_doctor(monkeypatch, _Proc(returncode=1, stdout=stdout))
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
        _patch_doctor(
            monkeypatch, _Proc(returncode=1, stdout="fatal error: something crashed")
        )
        hard, _soft = pf._check_cw_doctor()
        assert hard["passed"] is False

    def test_json_parse_failure_any_exit_is_unhealthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If cw doctor --json output is unparseable → always hard fail (any rc).

        Malformed stdout means doctor itself is broken regardless of exit code.
        """
        _patch_doctor(monkeypatch, _Proc(stdout="not json"))
        hard, soft = pf._check_cw_doctor()
        assert hard["passed"] is False
        assert "unparseable" in hard["detail"]
        assert soft["passed"] is False

    def test_non_object_json_is_unhealthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valid JSON that is not an object (no ``.get``) is a hard failure."""
        _patch_doctor(monkeypatch, _Proc(stdout="[1, 2]"))
        hard, soft = pf._check_cw_doctor()
        assert hard["passed"] is False
        assert "unparseable" in hard["detail"]
        assert soft["passed"] is False

    def test_stale_cw_build_rc2_degrades_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Click's usage-error exit code → degraded pass, not a hard block.

        Click exits with ``click.UsageError.exit_code`` and 'Error: No such
        option: --json' when the flag is unrecognized. The backend may still
        be healthy; do not block the smoke test on a build-version mismatch.
        """
        _patch_doctor(
            monkeypatch,
            _Proc(
                returncode=click.UsageError.exit_code,
                stderr="Error: No such option: --json",
            ),
        )
        hard, soft = pf._check_cw_doctor()
        assert hard["passed"] is True, "stale build must not hard-block backend check"
        assert "stale cw build" in hard["detail"]
        assert soft["passed"] is False

    def test_stale_build_branch_matches_real_click_unknown_option_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The degraded branch keys off the exit code Click really returns.

        Invoking a real cw command with an unknown flag pins that Click's
        unknown-option exit equals ``click.UsageError.exit_code``, so the
        stale-build detection tracks the dependency rather than a literal.
        """
        real = CliRunner().invoke(main, ["doctor", "--no-such-flag"])
        assert real.exit_code == click.UsageError.exit_code
        _patch_doctor(monkeypatch, _Proc(returncode=real.exit_code))
        hard, _soft = pf._check_cw_doctor()
        assert hard["passed"] is True
        assert "unsupported" in hard["detail"]

    def test_other_nonzero_exit_is_not_the_stale_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-usage-error exit with no JSON is a parse failure, not 'stale'."""
        _patch_doctor(
            monkeypatch, _Proc(returncode=click.UsageError.exit_code + 1, stdout="")
        )
        hard, _soft = pf._check_cw_doctor()
        assert hard["passed"] is False
        assert "stale cw build" not in hard["detail"]

    def test_daemon_reachable_failure_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """daemon-reachable ok=False → cw_backend_healthy fails.

        Note: current doctor.py uses warn=True (not ok=False) for daemon health;
        this test uses a synthetic payload to verify allowlist membership.
        """
        failing_checks: list[dict[str, object]] = [
            {
                "name": "daemon-reachable",
                "ok": False,
                "warn": False,
                "detail": "no response from daemon",
            }
        ]
        stdout = _make_doctor_json(ok=False, clean=False, checks=failing_checks)
        _patch_doctor(monkeypatch, _Proc(returncode=1, stdout=stdout))
        hard, soft = pf._check_cw_doctor()
        assert hard["passed"] is False
        assert soft["passed"] is False


def _queue_check(passed: bool, detail: str) -> dict[str, object]:
    return {
        "name": "not_already_queued",
        "passed": passed,
        "severity": "hard",
        "detail": detail,
    }


def _patch_queue_runs(monkeypatch: pytest.MonkeyPatch, procs: list[_Proc]) -> None:
    """Make successive ``subprocess.run`` calls return *procs* in order."""
    remaining = list(procs)

    def _fake_run(*_a: object, **_k: object) -> _Proc:
        return remaining.pop(0)

    monkeypatch.setattr(pf.shutil, "which", lambda _name: "/usr/bin/cw")
    monkeypatch.setattr(pf.subprocess, "run", _fake_run)


class TestCheckNotQueued:
    """Every branch of _check_not_queued keeps its pre-relocation result (#2697)."""

    def test_no_cw_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pf.shutil, "which", lambda _name: None)
        assert pf._check_not_queued("7", "acme") == _queue_check(
            passed=False, detail="cw binary not on PATH"
        )

    def test_status_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_queue_runs(
            monkeypatch,
            [_Proc(returncode=1), _Proc(returncode=1, stderr="  boom  ")],
        )
        assert pf._check_not_queued("7", "acme") == _queue_check(
            passed=False, detail="cw dev-queue status failed: boom"
        )

    def test_status_failure_falls_back_to_stdout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_queue_runs(
            monkeypatch,
            [_Proc(returncode=1), _Proc(returncode=1, stdout="out-only")],
        )
        assert pf._check_not_queued("7", "acme") == _queue_check(
            passed=False, detail="cw dev-queue status failed: out-only"
        )

    def test_plain_text_fallback_hit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_queue_runs(
            monkeypatch,
            [_Proc(returncode=1), _Proc(stdout="acme  7  RUNNING\n")],
        )
        assert pf._check_not_queued("7", "acme") == _queue_check(
            passed=False,
            detail="ticket 7 appears with state RUNNING in dev-queue status output",
        )

    def test_plain_text_fallback_miss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_queue_runs(
            monkeypatch,
            [_Proc(returncode=1), _Proc(stdout="acme  8  DONE\n")],
        )
        assert pf._check_not_queued("7", "acme") == _queue_check(
            passed=True, detail="ticket not seen in dev-queue status"
        )

    def test_json_parse_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_queue_runs(monkeypatch, [_Proc(stdout="not json")])
        result = pf._check_not_queued("7", "acme")
        assert result["passed"] is False
        assert result["severity"] == "hard"
        assert result["name"] == "not_already_queued"
        assert result["detail"].startswith("cw dev-queue status JSON parse failed: ")

    def test_json_hit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {
            "acme": [
                "not-a-dict",
                {"ticket_id": 8, "state": "RUNNING"},
                {"ticket_id": 7, "state": "PENDING"},
            ]
        }
        _patch_queue_runs(monkeypatch, [_Proc(stdout=json.dumps(payload))])
        assert pf._check_not_queued("7", "acme") == _queue_check(
            passed=False, detail="ticket 7 is PENDING in dev-queue for client acme"
        )

    @pytest.mark.parametrize(
        "payload",
        [
            {"acme": [{"ticket_id": 7, "state": "DONE"}]},
            {"other": [{"ticket_id": 7, "state": "RUNNING"}]},
            {"acme": "not-a-list"},
            ["not", "a", "dict"],
        ],
    )
    def test_json_miss(self, monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
        _patch_queue_runs(monkeypatch, [_Proc(stdout=json.dumps(payload))])
        assert pf._check_not_queued("7", "acme") == _queue_check(
            passed=True, detail="ticket 7 not queued or running for client acme"
        )


class TestResolveRepoRoot:
    """_resolve_repo_root: nearest cwd ancestor holding ``.git``, else cwd."""

    def test_main_checkout_root_from_nested_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = init_repo_with_remote(tmp_path / "repo", None)
        nested = repo / "src" / "pkg"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert pf._resolve_repo_root() == repo.resolve()

    def test_linked_worktree_where_git_is_a_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        make_git_repo: Callable[..., Path],
    ) -> None:
        repo = make_git_repo("main-checkout")
        worktree = tmp_path / "linked"
        git_in(repo, "worktree", "add", "-b", "feature", str(worktree))
        assert (worktree / ".git").is_file()
        nested = worktree / "deep"
        nested.mkdir()
        monkeypatch.chdir(nested)
        assert pf._resolve_repo_root() == worktree.resolve()

    def test_no_git_ancestor_falls_back_to_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The filesystem root has no ``.git`` ancestor (the same host
        # assumption as the ``ancestor_free_dir`` fixture), whereas tmp_path
        # may sit inside a dispatch worktree.
        root = Path(tmp_path.anchor)
        assert not (root / ".git").exists()
        monkeypatch.chdir(root)
        assert pf._resolve_repo_root() == root


class TestResolveClientRepoRoot:
    """_resolve_client_repo_root maps --client to a filesystem repo root (#2158)."""

    def test_resolves_workspace_path_client(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        client_root = tmp_path / "acme"
        client_root.mkdir()
        write_clients_yaml(ClientSpec("acme", workspace_path=client_root))
        assert pf._resolve_client_repo_root("acme") == client_root

    def test_resolves_repo_path_over_workspace_path(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        repo_root = tmp_path / "acme-repo"
        repo_root.mkdir()
        write_clients_yaml(ClientSpec("acme", repo_path=repo_root, branch="dev/123"))
        assert pf._resolve_client_repo_root("acme") == repo_root

    def test_dangling_client_raises(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        other_root = tmp_path / "other"
        other_root.mkdir()
        write_clients_yaml(ClientSpec("other-client", workspace_path=other_root))
        with pytest.raises(pf._ClientRepoUnresolvedError) as exc_info:
            pf._resolve_client_repo_root("missing-client")
        assert "missing-client" in str(exc_info.value)
        assert "clients.yaml" in str(exc_info.value)

    def test_no_clients_yaml_falls_back_to_resolve_repo_root(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = Path("/sentinel/repo/root")
        monkeypatch.setattr(pf, "_resolve_repo_root", lambda: sentinel)
        assert pf._resolve_client_repo_root("anything") == sentinel

    def test_malformed_yaml_raises_client_repo_unresolved(
        self, tmp_config_dir: Path
    ) -> None:
        """Not-even-valid-YAML clients.yaml must not crash preflight (#2158)."""
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
        repo = init_repo_with_remote(
            tmp_path / "widgets", "git@github.com:acme/widgets.git"
        )
        assert pf._resolve_repo_slug(repo) == "acme/widgets"

    def test_none_when_unresolvable(self, tmp_path: Path) -> None:
        repo = init_repo_with_remote(tmp_path / "no-remote", None)
        assert pf._resolve_repo_slug(repo) is None


def _write_raw_clients_yaml(tmp_config_dir: Path, content: bytes) -> None:
    config_dir = tmp_config_dir / ".config" / "cw"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "clients.yaml").write_bytes(content)


def _assert_single_client_unresolved(report: pf.PreflightReport, rc: int) -> None:
    assert rc == 1
    assert report["ok"] is False
    assert len(report["checks"]) == 1
    check = report["checks"][0]
    assert check["name"] == "client_repo_resolved"
    assert check["passed"] is False
    assert check["severity"] == "hard"
    assert "clients.yaml" in check["detail"]


class TestRunPreflight:
    """run_preflight's end-to-end wiring of client-derived repo/tracker (#2158)."""

    def test_client_repo_root_used_for_agents_and_tracker_not_cwd(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client_root = tmp_path / "client-root"
        _make_agent_repo(client_root, tracker="github-issues")
        decoy_root = tmp_path / "decoy-root"
        decoy_root.mkdir()

        write_clients_yaml(ClientSpec("acme", workspace_path=client_root))
        monkeypatch.setattr(pf, "_resolve_repo_root", lambda: decoy_root)
        monkeypatch.setattr(pf, "_resolve_repo_slug", lambda _root: "acme/widgets")

        captured: dict[str, tuple[str, ...]] = {}
        _install_check_stubs(pf, monkeypatch, captured)

        report, rc = pf.run_preflight("1", "acme", None)

        assert rc == 0
        assert report["ok"] is True
        assert report["repo_root"] == str(client_root)
        agents_check = next(
            c for c in report["checks"] if c["name"] == "agents_present"
        )
        assert agents_check["passed"] is True
        assert captured["ticket_open"][2] == "github-issues"
        assert captured["not_queued"] == ("1", "acme")

    def test_report_and_check_keys_unchanged(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The JSON contract keeps the script's keys (#2697)."""
        client_root = _make_agent_repo(tmp_path / "client-root")
        write_clients_yaml(ClientSpec("acme", workspace_path=client_root))
        monkeypatch.setattr(pf, "_resolve_repo_slug", lambda _root: "acme/widgets")
        _install_check_stubs(pf, monkeypatch, {})

        report, _rc = pf.run_preflight("1", "acme", None)

        assert set(report) == _REPORT_KEYS
        assert pf.PreflightReport.__required_keys__ == _REPORT_KEYS
        assert pf.PreflightCheck.__required_keys__ == _CHECK_KEYS
        assert [c["name"] for c in report["checks"]] == [
            "agents_present",
            "cw_backend_healthy",
            "cw_doctor_clean",
            "ticket_open",
            "no_open_pr_for_ticket",
            "not_already_queued",
        ]
        for check in report["checks"]:
            assert set(check) == _CHECK_KEYS
        json.dumps(report)

    def test_no_clients_yaml_uses_cwd_repo_root(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Single-tenant mode resolves the repo from cwd's nearest ``.git``."""
        repo = init_repo_with_remote(
            tmp_path / "widgets", "git@github.com:acme/widgets.git"
        )
        _make_agent_repo(repo, tracker="github-issues")
        nested = repo / "nested"
        nested.mkdir()
        monkeypatch.chdir(nested)
        captured: dict[str, tuple[str, ...]] = {}
        _install_check_stubs(pf, monkeypatch, captured)

        report, rc = pf.run_preflight("5", "acme", None)

        assert rc == 0, report
        assert report["repo_root"] == str(repo.resolve())
        assert report["repo"] == "acme/widgets"
        assert report["tracker"] == "github-issues"
        assert captured["ticket_open"] == ("5", "acme/widgets", "github-issues")

    def test_repo_flag_overrides_client_derived_slug(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client_root = tmp_path / "client-root"
        client_root.mkdir()
        monkeypatch.setattr(
            pf, "_resolve_client_repo_root", lambda _client: client_root
        )
        monkeypatch.setattr(pf, "_resolve_repo_slug", lambda _root: "derived/repo")

        captured: dict[str, tuple[str, ...]] = {}
        _install_check_stubs(pf, monkeypatch, captured)

        report, _rc = pf.run_preflight("1", "acme", "explicit/repo")

        assert report["repo"] == "explicit/repo"
        assert captured["ticket_open"][1] == "explicit/repo"
        assert captured["no_open_pr"][1] == "explicit/repo"

    def test_client_derives_repo_when_repo_omitted(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client_root = tmp_path / "client-root"
        client_root.mkdir()
        monkeypatch.setattr(
            pf, "_resolve_client_repo_root", lambda _client: client_root
        )
        monkeypatch.setattr(pf, "_resolve_repo_slug", lambda _root: "derived/repo")

        captured: dict[str, tuple[str, ...]] = {}
        _install_check_stubs(pf, monkeypatch, captured)

        pf.run_preflight("1", "acme", None)

        assert captured["ticket_open"][1] == "derived/repo"
        assert captured["no_open_pr"][1] == "derived/repo"

    def test_hard_failure_returns_rc_1(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client_root = tmp_path / "client-root"
        client_root.mkdir()  # no agents: agents_present fails hard
        monkeypatch.setattr(
            pf, "_resolve_client_repo_root", lambda _client: client_root
        )
        monkeypatch.setattr(pf, "_resolve_repo_slug", lambda _root: "acme/widgets")
        _install_check_stubs(pf, monkeypatch, {})

        report, rc = pf.run_preflight("1", "acme", None)

        assert rc == 1
        assert report["ok"] is False
        agents = next(c for c in report["checks"] if c["name"] == "agents_present")
        assert agents["passed"] is False
        assert agents["detail"].startswith("missing: plan-reviewer.md")

    def test_soft_failure_alone_keeps_ok(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client_root = _make_agent_repo(tmp_path / "client-root")
        monkeypatch.setattr(
            pf, "_resolve_client_repo_root", lambda _client: client_root
        )
        monkeypatch.setattr(pf, "_resolve_repo_slug", lambda _root: "acme/widgets")
        _install_check_stubs(pf, monkeypatch, {})
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
                    "passed": False,
                    "severity": "soft",
                    "detail": "warned",
                },
            ),
        )

        report, rc = pf.run_preflight("1", "acme", None)

        assert rc == 0
        assert report["ok"] is True

    def test_dangling_client_exits_1_with_single_check(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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

        report, rc = pf.run_preflight("1", "acme", "explicit/repo")

        assert rc == 1
        assert report == {
            "ok": False,
            "ticket_id": "1",
            "client": "acme",
            "repo": "explicit/repo",
            "repo_root": None,
            "tracker": None,
            "checks": [
                {
                    "name": "client_repo_resolved",
                    "passed": False,
                    "severity": "hard",
                    "detail": "boom",
                }
            ],
        }

    def test_unresolvable_repo_slug_without_override_hard_fails_and_skips_gh_checks(
        self,
        tmp_config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client_root = tmp_path / "client-root"
        client_root.mkdir()
        monkeypatch.setattr(
            pf, "_resolve_client_repo_root", lambda _client: client_root
        )
        monkeypatch.setattr(pf, "_resolve_repo_slug", lambda _root: None)
        _install_check_stubs(pf, monkeypatch, {})

        def _must_not_call(*_a: object, **_k: object) -> object:
            msg = "must not be called when repo slug is unresolved"
            raise AssertionError(msg)

        monkeypatch.setattr(pf, "_check_ticket_open", _must_not_call)
        monkeypatch.setattr(pf, "_check_no_open_pr", _must_not_call)

        report, rc = pf.run_preflight("1", "acme", None)

        assert rc == 1
        assert report["repo"] is None
        repo_check = next(c for c in report["checks"] if c["name"] == "repo_resolved")
        assert repo_check["passed"] is False
        assert repo_check["severity"] == "hard"
        assert "--repo OWNER/NAME" in repo_check["detail"]

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(
                b"clients:\n  acme:\n    workspace_path: [unterminated\n",
                id="malformed-yaml",
            ),
            pytest.param(
                b"clients:\n  acme:\n    workspace_path: /tmp/acme\n"
                b"    not_a_real_field: true\n",
                id="schema-invalid",
            ),
            # A 'clients:' key whose value is a list, not a mapping, is a
            # ConfigValidationError from load_clients() (#2158).
            pytest.param(b"clients:\n  - acme\n  - beta\n", id="structural"),
            # Real non-UTF-8 bytes make read_text() raise UnicodeDecodeError.
            pytest.param(b"\xff\xfe\x00\x01", id="non-utf8"),
        ],
    )
    def test_bad_clients_yaml_hard_fails_with_structured_report(
        self, tmp_config_dir: Path, content: bytes
    ) -> None:
        """A bad clients.yaml must not crash run_preflight (#2158)."""
        _write_raw_clients_yaml(tmp_config_dir, content)

        report, rc = pf.run_preflight("1", "acme", None)

        _assert_single_client_unresolved(report, rc)

    def test_unreadable_clients_yaml_hard_fails_with_structured_report(
        self, tmp_config_dir: Path
    ) -> None:
        """clients.yaml as a directory raises a genuine IsADirectoryError (#2158)."""
        config_dir = tmp_config_dir / ".config" / "cw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "clients.yaml").mkdir()

        report, rc = pf.run_preflight("1", "acme", None)

        _assert_single_client_unresolved(report, rc)

    def test_empty_clients_yaml_hard_fails_without_fallback(
        self,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An existing, empty clients.yaml with a client must fail loudly,
        never silently fall back to _resolve_repo_root() (#2158)."""
        _write_raw_clients_yaml(tmp_config_dir, b"")

        def _fail_if_called() -> Path:
            msg = "_resolve_repo_root() must not be called for a populated file"
            raise AssertionError(msg)

        monkeypatch.setattr(pf, "_resolve_repo_root", _fail_if_called)

        report, rc = pf.run_preflight("1", "acme", None)

        _assert_single_client_unresolved(report, rc)
        assert "acme" in report["checks"][0]["detail"]
