"""Tests for ``prep_pr_state.py gate-ci-evidence`` (open-PR reuse path).

The command is opt-in per repo (``quality_gates.ci_evidence``, read from the
committed config on ``origin/<base>``) and fails closed: only an open PR on the
expected base, whose live head equals local HEAD, with every mapped check
finished green on that commit, yields ``skip: true``. Every other outcome must
report ``skip: false`` so the caller runs the gate locally.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any, cast

import pytest

from tests.test_prep_pr_state import _mod

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
GATE = "unit-tests"
CHECK = "ci-status"

_CONFIG = f"""\
quality_gates:
  ci_evidence:
    {GATE}: {CHECK}
"""


def _git_returning(value: str) -> Callable[..., str]:
    """A ``_run_git`` stand-in that answers *value* to every call."""

    def fake_git(args: list[str], default: str = "") -> str:
        del args, default
        return value

    return fake_git


def _config_returning(text: str) -> Callable[[str], str]:
    """A ``_base_config_text`` stand-in that answers *text* for any base."""

    def fake_config(base: str) -> str:
        del base
        return text

    return fake_config


def _const(value: object) -> Callable[[list[str]], object]:
    """A ``_gh_json`` stand-in that answers *value* for any arguments."""

    def fake_gh(args: list[str]) -> object:
        del args
        return value

    return fake_gh


def _check_run(
    name: str, *, status: str = "completed", conclusion: str = "success"
) -> dict[str, Any]:
    """A REST check run (lower-case enums, as ``gh api`` returns them)."""
    return {"name": name, "status": status, "conclusion": conclusion}


def _status(name: str, state: str) -> dict[str, Any]:
    """A REST commit status."""
    return {"context": name, "state": state}


def _pr(*, head: str = HEAD, base: str = "main", state: str = "OPEN") -> dict[str, Any]:
    """``gh pr view --json headRefOid,baseRefName,state`` (GraphQL, upper-case)."""
    return {"headRefOid": head, "baseRefName": base, "state": state}


class _Github:
    """Stands in for the two GitHub reads and records how they were called."""

    def __init__(
        self,
        pr: dict[str, Any] | None,
        entries: dict[str, list[dict[str, Any]] | None] | None = None,
    ) -> None:
        self.pr = pr
        self.entries = entries or {}
        self.pr_calls: list[tuple[int, str | None]] = []
        self.entry_calls: list[tuple[str | None, str, str]] = []

    def pr_state(self, pr: int, repo: str | None) -> dict[str, Any] | None:
        self.pr_calls.append((pr, repo))
        return self.pr

    def check_entries(
        self, repo: str | None, sha: str, check: str
    ) -> list[dict[str, Any]] | None:
        self.entry_calls.append((repo, sha, check))
        return self.entries.get(check, [])


@pytest.fixture
def project(monkeypatch: pytest.MonkeyPatch) -> None:
    """origin/main carries a map of GATE -> CHECK; local HEAD is HEAD."""
    monkeypatch.setattr(_mod, "_base_config_text", _config_returning(_CONFIG))
    monkeypatch.setattr(_mod, "_run_git", _git_returning(HEAD))


def _evidence(
    monkeypatch: pytest.MonkeyPatch,
    github: _Github,
    *,
    gate: str = GATE,
    repo: str | None = None,
    base: str = "main",
) -> dict[str, Any]:
    monkeypatch.setattr(_mod, "_pr_state", github.pr_state)
    monkeypatch.setattr(_mod, "_check_entries", github.check_entries)
    return cast("dict[str, Any]", _mod.gate_ci_evidence(gate, 7, repo, base))


@pytest.mark.usefixtures("project")
class TestMatched:
    def test_green_check_at_live_head_skips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        github = _Github(_pr(), {CHECK: [_check_run(CHECK)]})
        verdict = _evidence(monkeypatch, github)
        assert verdict["skip"] is True
        assert verdict["reason"] == "matched"
        assert verdict["sha"] == HEAD
        assert verdict["checks"] == [CHECK]
        assert github.pr_calls == [(7, None)]
        assert github.entry_calls == [(None, HEAD, CHECK)]

    def test_commit_status_success_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        github = _Github(_pr(), {CHECK: [_status(CHECK, "success")]})
        assert _evidence(monkeypatch, github)["skip"] is True

    def test_graphql_upper_case_enums_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = _check_run(CHECK, status="COMPLETED", conclusion="SUCCESS")
        assert _evidence(monkeypatch, _Github(_pr(), {CHECK: [run]}))["skip"] is True

    def test_list_entry_needs_every_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            _mod,
            "_base_config_text",
            _config_returning(
                f"quality_gates:\n  ci_evidence:\n    {GATE}: [{CHECK}, second]\n"
            ),
        )
        both = _Github(
            _pr(), {CHECK: [_check_run(CHECK)], "second": [_check_run("second")]}
        )
        assert _evidence(monkeypatch, both)["skip"] is True
        one = _Github(_pr(), {CHECK: [_check_run(CHECK)]})
        verdict = _evidence(monkeypatch, one)
        assert verdict["skip"] is False
        assert verdict["reason"] == "check-missing"
        assert verdict["failed_check"] == "second"

    def test_repo_and_base_are_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def base_config(base: str) -> str:
            seen.append(base)
            return _CONFIG

        monkeypatch.setattr(_mod, "_base_config_text", base_config)
        github = _Github(_pr(base="develop"), {CHECK: [_check_run(CHECK)]})
        verdict = _evidence(monkeypatch, github, repo="owner/name", base="develop")
        assert verdict["skip"] is True
        assert seen == ["develop"]
        assert github.pr_calls == [(7, "owner/name")]
        assert github.entry_calls == [("owner/name", HEAD, CHECK)]


@pytest.mark.usefixtures("project")
class TestFailClosed:
    def test_no_map_entry_for_gate_never_calls_github(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        github = _Github(_pr(), {CHECK: [_check_run(CHECK)]})
        verdict = _evidence(monkeypatch, github, gate="not-mapped")
        assert verdict["skip"] is False
        assert verdict["reason"] == "no-map-entry"
        assert github.pr_calls == []
        assert github.entry_calls == []

    @pytest.mark.parametrize(
        "config",
        ["", "quality_gates: [unclosed\n", "- a\n- list\n"],
        ids=["absent", "malformed", "non-mapping"],
    )
    def test_unusable_base_config_is_no_map(
        self, monkeypatch: pytest.MonkeyPatch, config: str
    ) -> None:
        monkeypatch.setattr(_mod, "_base_config_text", _config_returning(config))
        github = _Github(_pr(), {CHECK: [_check_run(CHECK)]})
        verdict = _evidence(monkeypatch, github)
        assert verdict["skip"] is False
        assert verdict["reason"] == "no-map-entry"
        assert github.pr_calls == []

    def test_without_pyyaml_nothing_is_skipped_and_the_reason_says_why(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_mod, "yaml", None)
        github = _Github(_pr(), {CHECK: [_check_run(CHECK)]})
        verdict = _evidence(monkeypatch, github)
        assert verdict["skip"] is False
        assert verdict["reason"] == "pyyaml-unavailable"
        assert github.pr_calls == []

    def test_unresolvable_local_head(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_mod, "_run_git", _git_returning(""))
        github = _Github(_pr(), {CHECK: [_check_run(CHECK)]})
        verdict = _evidence(monkeypatch, github)
        assert verdict["skip"] is False
        assert verdict["reason"] == "head-unresolved"
        assert verdict["sha"] is None
        assert github.pr_calls == []

    def test_github_unreachable_for_pr_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        verdict = _evidence(monkeypatch, _Github(None))
        assert verdict["skip"] is False
        assert verdict["reason"] == "ci-signal-unavailable"

    @pytest.mark.parametrize("state", ["MERGED", "CLOSED", "", None])
    def test_pr_not_open(
        self, monkeypatch: pytest.MonkeyPatch, state: str | None
    ) -> None:
        github = _Github(
            {"headRefOid": HEAD, "baseRefName": "main", "state": state},
            {CHECK: [_check_run(CHECK)]},
        )
        verdict = _evidence(monkeypatch, github)
        assert verdict["skip"] is False
        assert verdict["reason"] == "pr-not-open"
        assert github.entry_calls == []

    def test_pr_retargeted_to_another_base(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        github = _Github(_pr(base="release"), {CHECK: [_check_run(CHECK)]})
        verdict = _evidence(monkeypatch, github)
        assert verdict["skip"] is False
        assert verdict["reason"] == "pr-base-mismatch"
        assert verdict["live_base"] == "release"
        assert github.entry_calls == []

    def test_head_moved_since_ci_ran_is_pinned_to_live_head(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        github = _Github(_pr(head=OTHER_HEAD), {CHECK: [_check_run(CHECK)]})
        verdict = _evidence(monkeypatch, github)
        assert verdict["skip"] is False
        assert verdict["reason"] == "head-mismatch"
        assert verdict["sha"] == HEAD
        assert verdict["live_sha"] == OTHER_HEAD
        assert github.entry_calls == []

    def test_missing_live_head_is_a_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        github = _Github(
            {"baseRefName": "main", "state": "OPEN"}, {CHECK: [_check_run(CHECK)]}
        )
        verdict = _evidence(monkeypatch, github)
        assert verdict["skip"] is False
        assert verdict["reason"] == "head-mismatch"

    def test_check_lookup_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        verdict = _evidence(monkeypatch, _Github(_pr(), {CHECK: None}))
        assert verdict["skip"] is False
        assert verdict["reason"] == "ci-signal-unavailable"
        assert verdict["failed_check"] == CHECK

    def test_mapped_check_has_no_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        verdict = _evidence(monkeypatch, _Github(_pr(), {CHECK: []}))
        assert verdict["skip"] is False
        assert verdict["reason"] == "check-missing"
        assert verdict["failed_check"] == CHECK

    @pytest.mark.parametrize(
        ("status", "conclusion"),
        [
            ("completed", "failure"),
            ("completed", "cancelled"),
            ("completed", "timed_out"),
            ("completed", "skipped"),
            ("completed", "neutral"),
            ("completed", "action_required"),
            ("completed", "stale"),
            ("in_progress", ""),
            ("queued", ""),
            ("in_progress", "success"),
            ("completed", ""),
        ],
    )
    def test_check_run_not_a_finished_success(
        self, monkeypatch: pytest.MonkeyPatch, status: str, conclusion: str
    ) -> None:
        run = _check_run(CHECK, status=status, conclusion=conclusion)
        verdict = _evidence(monkeypatch, _Github(_pr(), {CHECK: [run]}))
        assert verdict["skip"] is False
        assert verdict["reason"] == "check-not-success"
        assert verdict["failed_check"] == CHECK

    @pytest.mark.parametrize("state", ["pending", "failure", "error", "expected"])
    def test_commit_status_not_success(
        self, monkeypatch: pytest.MonkeyPatch, state: str
    ) -> None:
        github = _Github(_pr(), {CHECK: [_status(CHECK, state)]})
        verdict = _evidence(monkeypatch, github)
        assert verdict["skip"] is False
        assert verdict["reason"] == "check-not-success"

    def test_one_red_entry_among_same_named_entries_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entries = [_check_run(CHECK), _status(CHECK, "failure")]
        verdict = _evidence(monkeypatch, _Github(_pr(), {CHECK: entries}))
        assert verdict["skip"] is False
        assert verdict["reason"] == "check-not-success"


class TestBaseConfigIsAuthoritative:
    def test_working_tree_map_is_ignored(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_dir = tmp_path / ".claude"
        config_dir.mkdir()
        (config_dir / "project-config.yaml").write_text(_CONFIG, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(_mod, "_base_config_text", _config_returning(""))
        monkeypatch.setattr(_mod, "_run_git", _git_returning(HEAD))
        github = _Github(_pr(), {CHECK: [_check_run(CHECK)]})
        verdict = _evidence(monkeypatch, github)
        assert verdict["skip"] is False
        assert verdict["reason"] == "no-map-entry"
        assert github.pr_calls == []

    def _git_show(
        self, monkeypatch: pytest.MonkeyPatch, outcome: object
    ) -> tuple[str, list[list[str]]]:
        seen: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            seen.append(cmd)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        monkeypatch.setattr(_mod.subprocess, "run", fake_run)
        return _mod._base_config_text("main"), seen

    def test_reads_the_committed_file_on_origin_base(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        done = subprocess.CompletedProcess([], 0, stdout=_CONFIG, stderr="")
        text, seen = self._git_show(monkeypatch, done)
        assert text == _CONFIG
        assert seen == [["git", "show", "origin/main:.claude/project-config.yaml"]]

    @pytest.mark.parametrize(
        "outcome",
        [
            subprocess.CompletedProcess([], 128, stdout="partial", stderr="missing"),
            FileNotFoundError("git"),
            PermissionError("git"),
            subprocess.TimeoutExpired("git", 60),
        ],
        ids=["not-in-tree", "git-missing", "permission", "timeout"],
    )
    def test_any_failure_reads_as_no_config(
        self, monkeypatch: pytest.MonkeyPatch, outcome: object
    ) -> None:
        text, _ = self._git_show(monkeypatch, outcome)
        assert text == ""


class TestParseCiEvidenceMap:
    def test_valid_entries_string_and_list(self) -> None:
        loaded = _mod.parse_ci_evidence_map(
            "quality_gates:\n"
            "  ci_evidence:\n"
            "    lint: lint-job\n"
            "    tests: [shard-a, shard-b]\n",
            "test",
        )
        assert loaded == {"lint": ["lint-job"], "tests": ["shard-a", "shard-b"]}

    def test_invalid_entries_are_dropped_without_hurting_siblings(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            loaded = _mod.parse_ci_evidence_map(
                "quality_gates:\n"
                "  ci_evidence:\n"
                "    good: ok-check\n"
                "    empty-list: []\n"
                "    blank: '  '\n"
                "    number: 3\n"
                "    mixed: [fine, 4]\n"
                "    5: numeric-key\n",
                "origin/main",
            )
        assert loaded == {"good": ["ok-check"]}
        assert "empty-list" in caplog.text
        assert "mixed" in caplog.text
        assert "origin/main" in caplog.text

    @pytest.mark.parametrize(
        "text",
        [
            "quality_gates:\n  timeouts:\n    tests: 300\n",
            "- just\n- a list\n",
            "",
            "quality_gates: [unclosed\n",
            "quality_gates: scalar\n",
        ],
        ids=[
            "timeouts-only",
            "non-mapping-root",
            "empty",
            "malformed",
            "non-mapping-section",
        ],
    )
    def test_no_usable_map(self, text: str) -> None:
        assert _mod.parse_ci_evidence_map(text, "test") == {}

    def test_deeply_nested_yaml_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(text: str) -> object:
            raise RecursionError(text)

        monkeypatch.setattr(_mod, "_parse_ci_evidence", boom)
        assert _mod.parse_ci_evidence_map("x", "test") == {}

    def test_without_pyyaml_the_map_is_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_mod, "yaml", None)
        assert _mod.parse_ci_evidence_map(_CONFIG, "test") == {}

    def test_evidence_map_does_not_disturb_timeout_overrides(
        self, tmp_path: Path
    ) -> None:
        text = (
            "quality_gates:\n"
            "  timeouts:\n"
            "    tests: 300\n"
            "  ci_evidence:\n"
            "    tests: ci-status\n"
        )
        config_dir = tmp_path / ".claude"
        config_dir.mkdir()
        config = config_dir / "project-config.yaml"
        config.write_text(text, encoding="utf-8")
        assert _mod.load_timeout_overrides(config) == {"tests": 300}
        assert _mod.parse_ci_evidence_map(text, "test") == {"tests": ["ci-status"]}

    @pytest.mark.parametrize(
        "body",
        [
            "  timeouts:\n    tests: 300\n  ci_evidence:\n    tests: ci-status\n",
            "  ci_evidence:\n    tests: ci-status\n  timeouts:\n    tests: 300\n",
        ],
        ids=["timeouts-first", "evidence-first"],
    )
    def test_bare_python_timeout_scanner_ignores_sibling_evidence_map(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        body: str,
    ) -> None:
        monkeypatch.setattr(_mod, "yaml", None)
        config_dir = tmp_path / ".claude"
        config_dir.mkdir()
        config = config_dir / "project-config.yaml"
        config.write_text(f"quality_gates:\n{body}", encoding="utf-8")
        assert _mod.load_timeout_overrides(config) == {"tests": 300}


class TestGhJson:
    def _run(
        self, monkeypatch: pytest.MonkeyPatch, outcome: object
    ) -> tuple[Any, list[list[str]]]:
        seen: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            seen.append(cmd)
            if isinstance(outcome, BaseException):
                raise outcome
            return subprocess.CompletedProcess(cmd, 0, stdout=outcome, stderr="")

        monkeypatch.setattr(_mod.subprocess, "run", fake_run)
        return _mod._gh_json(["pr", "view", "7"]), seen

    def test_parses_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result, seen = self._run(monkeypatch, json.dumps({"a": 1}))
        assert result == {"a": 1}
        assert seen == [["gh", "pr", "view", "7"]]

    @pytest.mark.parametrize(
        "outcome",
        [
            subprocess.CalledProcessError(1, "gh"),
            subprocess.TimeoutExpired("gh", 60),
            FileNotFoundError("gh"),
            PermissionError("gh"),
            NotADirectoryError("gh"),
            "not json",
        ],
        ids=[
            "gh-error",
            "timeout",
            "gh-missing",
            "permission",
            "not-a-dir",
            "bad-json",
        ],
    )
    def test_any_failure_is_none(
        self, monkeypatch: pytest.MonkeyPatch, outcome: object
    ) -> None:
        result, _ = self._run(monkeypatch, outcome)
        assert result is None


class TestPrState:
    def test_asks_for_head_base_and_state_and_forwards_repo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []

        def fake_gh(args: list[str]) -> object:
            seen.append(args)
            return _pr()

        monkeypatch.setattr(_mod, "_gh_json", fake_gh)
        assert _mod._pr_state(7, "owner/name") == _pr()
        assert seen[0][:4] == ["pr", "view", "7", "--json"]
        assert seen[0][4] == "headRefOid,baseRefName,state"
        assert seen[0][-2:] == ["--repo", "owner/name"]
        seen.clear()
        _mod._pr_state(7, None)
        assert "--repo" not in seen[0]

    @pytest.mark.parametrize("payload", [None, [1], "x"])
    def test_non_object_is_none(
        self, monkeypatch: pytest.MonkeyPatch, payload: object
    ) -> None:
        monkeypatch.setattr(_mod, "_gh_json", _const(payload))
        assert _mod._pr_state(7, None) is None


class TestCheckEntries:
    @staticmethod
    def _gh(
        runs: object, statuses: object, seen: list[list[str]]
    ) -> Callable[..., object]:
        def fake_gh(args: list[str]) -> object:
            seen.append(args)
            return runs if "check-runs" in args[3] else statuses

        return fake_gh

    def test_queries_by_name_on_the_exact_commit_and_merges_both_sources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []
        runs = {"total_count": 1, "check_runs": [_check_run(CHECK)]}
        statuses = {
            "total_count": 2,
            "statuses": [_status(CHECK, "success"), _status("other", "failure")],
        }
        monkeypatch.setattr(_mod, "_gh_json", self._gh(runs, statuses, seen))
        entries = _mod._check_entries("owner/name", HEAD, CHECK)
        assert entries == [_check_run(CHECK), _status(CHECK, "success")]
        run_args = next(a for a in seen if "check-runs" in a[3])
        assert run_args[3] == f"repos/owner/name/commits/{HEAD}/check-runs"
        assert "check_name=ci-status" in run_args
        assert "filter=latest" in run_args
        assert "per_page=100" in run_args

    def test_current_repo_placeholder_when_no_repo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []
        runs = {"total_count": 0, "check_runs": []}
        statuses = {"total_count": 0, "statuses": []}
        monkeypatch.setattr(_mod, "_gh_json", self._gh(runs, statuses, seen))
        assert _mod._check_entries(None, HEAD, CHECK) == []
        assert seen[0][3] == "repos/{owner}/{repo}/commits/" + HEAD + "/check-runs"

    def test_local_name_refilter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runs = {
            "total_count": 2,
            "check_runs": [_check_run(CHECK), _check_run("other")],
        }
        statuses = {"total_count": 0, "statuses": []}
        monkeypatch.setattr(_mod, "_gh_json", self._gh(runs, statuses, []))
        assert _mod._check_entries(None, HEAD, CHECK) == [_check_run(CHECK)]

    @pytest.mark.parametrize(
        ("runs", "statuses"),
        [
            (None, {"total_count": 0, "statuses": []}),
            ({"total_count": 0, "check_runs": []}, None),
            ([], {"total_count": 0, "statuses": []}),
            ({"total_count": 0}, {"total_count": 0, "statuses": []}),
            ({"total_count": 0, "check_runs": []}, {"total_count": 0}),
            (
                {"total_count": 101, "check_runs": [_check_run(CHECK)]},
                {"total_count": 0, "statuses": []},
            ),
            ({"check_runs": [_check_run(CHECK)]}, {"total_count": 0, "statuses": []}),
        ],
        ids=[
            "runs-failed",
            "status-failed",
            "runs-not-object",
            "no-runs-list",
            "no-statuses-list",
            "incomplete-page",
            "no-total",
        ],
    )
    def test_unknown_or_incomplete_is_none(
        self, monkeypatch: pytest.MonkeyPatch, runs: object, statuses: object
    ) -> None:
        monkeypatch.setattr(_mod, "_gh_json", self._gh(runs, statuses, []))
        assert _mod._check_entries(None, HEAD, CHECK) is None


class TestMainDispatch:
    def test_cli_prints_json_and_forwards_pr_repo_and_base(
        self,
        project: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        del project
        bases: list[str] = []

        def base_config(base: str) -> str:
            bases.append(base)
            return _CONFIG

        monkeypatch.setattr(_mod, "_base_config_text", base_config)
        github = _Github(_pr(base="develop"), {CHECK: [_check_run(CHECK)]})
        monkeypatch.setattr(_mod, "_pr_state", github.pr_state)
        monkeypatch.setattr(_mod, "_check_entries", github.check_entries)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "prep_pr_state.py",
                "gate-ci-evidence",
                GATE,
                "--pr",
                "7",
                "--repo",
                "o/r",
                "--base",
                "develop",
            ],
        )
        _mod.main()
        out = json.loads(capsys.readouterr().out)
        assert out["skip"] is True
        assert bases == ["develop"]
        assert github.pr_calls == [(7, "o/r")]

    def test_cli_base_defaults_to_main_and_a_miss_exits_zero(
        self,
        project: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        del project
        bases: list[str] = []

        def base_config(base: str) -> str:
            bases.append(base)
            return _CONFIG

        monkeypatch.setattr(_mod, "_base_config_text", base_config)
        monkeypatch.setattr(_mod, "_pr_state", _Github(None).pr_state)
        monkeypatch.setattr(
            sys, "argv", ["prep_pr_state.py", "gate-ci-evidence", GATE, "--pr", "7"]
        )
        _mod.main()
        out = json.loads(capsys.readouterr().out)
        assert out["skip"] is False
        assert out["reason"] == "ci-signal-unavailable"
        assert bases == ["main"]

    def test_cli_requires_pr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["prep_pr_state.py", "gate-ci-evidence", GATE])
        with pytest.raises(SystemExit) as excinfo:
            _mod.main()
        assert excinfo.value.code != 0
