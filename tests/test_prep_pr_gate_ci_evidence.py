"""Tests for ``prep_pr_state.py gate-ci-evidence`` (open-PR reuse path).

The command is opt-in per repo (``quality_gates.ci_evidence`` in
``.claude/project-config.yaml``) and fails closed: only a live-head match with
every mapped check finished green yields ``skip: true``. Every other outcome
must report ``skip: false`` so the caller runs the gate locally.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any, cast

import pytest

from tests._shared.cli_preflight import _write_project_config_yaml
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


def _check_run(
    name: str, *, status: str = "COMPLETED", conclusion: str = "SUCCESS"
) -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
    }


def _status_context(name: str, state: str) -> dict[str, Any]:
    return {"__typename": "StatusContext", "context": name, "state": state}


def _snapshot(*entries: dict[str, Any], head: str = HEAD) -> dict[str, Any]:
    return {"headRefOid": head, "statusCheckRollup": list(entries)}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """cwd with a project-config mapping GATE to CHECK; local HEAD is HEAD."""
    _write_project_config_yaml(tmp_path, _CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_mod, "_run_git", _git_returning(HEAD))
    return tmp_path


class _SnapshotSpy:
    """Stands in for ``_pr_ci_snapshot`` and records how it was called."""

    def __init__(self, result: dict[str, Any] | None) -> None:
        self.result = result
        self.calls: list[tuple[int, str | None]] = []

    def __call__(self, pr: int, repo: str | None) -> dict[str, Any] | None:
        self.calls.append((pr, repo))
        return self.result


def _evidence(
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, Any] | None,
    *,
    gate: str = GATE,
) -> tuple[dict[str, Any], _SnapshotSpy]:
    spy = _SnapshotSpy(result)
    monkeypatch.setattr(_mod, "_pr_ci_snapshot", spy)
    return cast("dict[str, Any]", _mod.gate_ci_evidence(gate, 7)), spy


class TestMatched:
    def test_green_check_at_live_head_skips(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del project
        verdict, spy = _evidence(monkeypatch, _snapshot(_check_run(CHECK)))
        assert verdict["skip"] is True
        assert verdict["reason"] == "matched"
        assert verdict["sha"] == HEAD
        assert verdict["checks"] == [CHECK]
        assert spy.calls == [(7, None)]

    def test_status_context_success_counts(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del project
        verdict, _ = _evidence(
            monkeypatch, _snapshot(_status_context(CHECK, "SUCCESS"))
        )
        assert verdict["skip"] is True

    def test_unrelated_red_check_does_not_block(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del project
        verdict, _ = _evidence(
            monkeypatch,
            _snapshot(_check_run(CHECK), _check_run("other", conclusion="FAILURE")),
        )
        assert verdict["skip"] is True

    def test_list_entry_needs_every_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_project_config_yaml(
            tmp_path,
            f"quality_gates:\n  ci_evidence:\n    {GATE}: [{CHECK}, second]\n",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(_mod, "_run_git", _git_returning(HEAD))
        both, _ = _evidence(
            monkeypatch, _snapshot(_check_run(CHECK), _check_run("second"))
        )
        assert both["skip"] is True
        one, _ = _evidence(monkeypatch, _snapshot(_check_run(CHECK)))
        assert one["skip"] is False
        assert one["reason"] == "check-missing"
        assert one["failed_check"] == "second"

    def test_repo_is_forwarded(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del project
        spy = _SnapshotSpy(_snapshot(_check_run(CHECK)))
        monkeypatch.setattr(_mod, "_pr_ci_snapshot", spy)
        _mod.gate_ci_evidence(GATE, 7, "owner/name")
        assert spy.calls == [(7, "owner/name")]


class TestFailClosed:
    def test_no_map_entry_for_gate_skips_nothing_and_never_calls_github(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del project
        verdict, spy = _evidence(
            monkeypatch, _snapshot(_check_run(CHECK)), gate="not-mapped"
        )
        assert verdict["skip"] is False
        assert verdict["reason"] == "no-map-entry"
        assert spy.calls == []

    def test_no_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(_mod, "_run_git", _git_returning(HEAD))
        verdict, spy = _evidence(monkeypatch, _snapshot(_check_run(CHECK)))
        assert verdict["reason"] == "no-map-entry"
        assert verdict["skip"] is False
        assert spy.calls == []

    def test_malformed_config_yields_empty_map(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_project_config_yaml(tmp_path, "quality_gates: [unclosed\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(_mod, "_run_git", _git_returning(HEAD))
        verdict, _ = _evidence(monkeypatch, _snapshot(_check_run(CHECK)))
        assert verdict["skip"] is False
        assert verdict["reason"] == "no-map-entry"

    def test_without_pyyaml_the_map_is_empty(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del project
        monkeypatch.setattr(_mod, "yaml", None)
        verdict, spy = _evidence(monkeypatch, _snapshot(_check_run(CHECK)))
        assert verdict["skip"] is False
        assert verdict["reason"] == "no-map-entry"
        assert spy.calls == []

    def test_unresolvable_local_head(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del project
        monkeypatch.setattr(_mod, "_run_git", _git_returning(""))
        verdict, spy = _evidence(monkeypatch, _snapshot(_check_run(CHECK)))
        assert verdict["skip"] is False
        assert verdict["reason"] == "head-unresolved"
        assert verdict["sha"] is None
        assert spy.calls == []

    def test_github_unreachable(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del project
        verdict, _ = _evidence(monkeypatch, None)
        assert verdict["skip"] is False
        assert verdict["reason"] == "ci-signal-unavailable"

    def test_head_moved_since_ci_ran_is_pinned_to_live_head(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del project
        verdict, _ = _evidence(
            monkeypatch, _snapshot(_check_run(CHECK), head=OTHER_HEAD)
        )
        assert verdict["skip"] is False
        assert verdict["reason"] == "head-mismatch"
        assert verdict["sha"] == HEAD
        assert verdict["live_sha"] == OTHER_HEAD

    def test_missing_live_head_is_a_mismatch(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del project
        verdict, _ = _evidence(monkeypatch, {"statusCheckRollup": [_check_run(CHECK)]})
        assert verdict["skip"] is False
        assert verdict["reason"] == "head-mismatch"

    @pytest.mark.parametrize(
        "rollup", [[], None, "nope", [None, "x"]], ids=["empty", "null", "str", "junk"]
    )
    def test_no_ci_signal(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, rollup: object
    ) -> None:
        del project
        verdict, _ = _evidence(
            monkeypatch, {"headRefOid": HEAD, "statusCheckRollup": rollup}
        )
        assert verdict["skip"] is False
        assert verdict["reason"] == "no-ci-signal"

    def test_mapped_check_absent_from_rollup(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del project
        verdict, _ = _evidence(monkeypatch, _snapshot(_check_run("other")))
        assert verdict["skip"] is False
        assert verdict["reason"] == "check-missing"
        assert verdict["failed_check"] == CHECK

    @pytest.mark.parametrize(
        ("status", "conclusion"),
        [
            ("COMPLETED", "FAILURE"),
            ("COMPLETED", "CANCELLED"),
            ("COMPLETED", "TIMED_OUT"),
            ("COMPLETED", "SKIPPED"),
            ("COMPLETED", "NEUTRAL"),
            ("IN_PROGRESS", ""),
            ("QUEUED", ""),
            ("IN_PROGRESS", "SUCCESS"),
        ],
    )
    def test_check_run_not_a_finished_success(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        status: str,
        conclusion: str,
    ) -> None:
        del project
        verdict, _ = _evidence(
            monkeypatch,
            _snapshot(_check_run(CHECK, status=status, conclusion=conclusion)),
        )
        assert verdict["skip"] is False
        assert verdict["reason"] == "check-not-success"
        assert verdict["failed_check"] == CHECK

    @pytest.mark.parametrize("state", ["PENDING", "FAILURE", "ERROR", "EXPECTED"])
    def test_status_context_not_success(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, state: str
    ) -> None:
        del project
        verdict, _ = _evidence(monkeypatch, _snapshot(_status_context(CHECK, state)))
        assert verdict["skip"] is False
        assert verdict["reason"] == "check-not-success"

    def test_one_red_entry_among_same_named_entries_blocks(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del project
        verdict, _ = _evidence(
            monkeypatch,
            _snapshot(_check_run(CHECK), _check_run(CHECK, conclusion="FAILURE")),
        )
        assert verdict["skip"] is False
        assert verdict["reason"] == "check-not-success"


class TestLoadCiEvidenceMap:
    def test_valid_entries_string_and_list(self, tmp_path: Path) -> None:
        _write_project_config_yaml(
            tmp_path,
            "quality_gates:\n"
            "  ci_evidence:\n"
            "    lint: lint-job\n"
            "    tests: [shard-a, shard-b]\n",
        )
        loaded = _mod.load_ci_evidence_map(tmp_path / ".claude" / "project-config.yaml")
        assert loaded == {"lint": ["lint-job"], "tests": ["shard-a", "shard-b"]}

    def test_invalid_entries_are_dropped_without_hurting_siblings(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_project_config_yaml(
            tmp_path,
            "quality_gates:\n"
            "  ci_evidence:\n"
            "    good: ok-check\n"
            "    empty-list: []\n"
            "    blank: '  '\n"
            "    number: 3\n"
            "    mixed: [fine, 4]\n",
        )
        with caplog.at_level("WARNING"):
            loaded = _mod.load_ci_evidence_map(
                tmp_path / ".claude" / "project-config.yaml"
            )
        assert loaded == {"good": ["ok-check"]}
        assert "empty-list" in caplog.text
        assert "mixed" in caplog.text

    def test_timeouts_only_config_has_no_evidence(self, tmp_path: Path) -> None:
        _write_project_config_yaml(
            tmp_path, "quality_gates:\n  timeouts:\n    tests: 300\n"
        )
        assert (
            _mod.load_ci_evidence_map(tmp_path / ".claude" / "project-config.yaml")
            == {}
        )

    def test_non_mapping_root_is_empty(self, tmp_path: Path) -> None:
        _write_project_config_yaml(tmp_path, "- just\n- a list\n")
        assert (
            _mod.load_ci_evidence_map(tmp_path / ".claude" / "project-config.yaml")
            == {}
        )

    def test_evidence_map_does_not_disturb_timeout_overrides(
        self, tmp_path: Path
    ) -> None:
        _write_project_config_yaml(
            tmp_path,
            "quality_gates:\n"
            "  timeouts:\n"
            "    tests: 300\n"
            "  ci_evidence:\n"
            "    tests: ci-status\n",
        )
        config = tmp_path / ".claude" / "project-config.yaml"
        assert _mod.load_timeout_overrides(config) == {"tests": 300}
        assert _mod.load_ci_evidence_map(config) == {"tests": ["ci-status"]}

    @pytest.mark.parametrize(
        "body",
        [
            "  timeouts:\n    tests: 300\n  ci_evidence:\n    tests: ci-status\n",
            "  ci_evidence:\n    tests: ci-status\n  timeouts:\n    tests: 300\n",
        ],
        ids=["timeouts-first", "evidence-first"],
    )
    def test_bare_python_timeout_scanner_ignores_sibling_evidence_map(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
    ) -> None:
        monkeypatch.setattr(_mod, "yaml", None)
        _write_project_config_yaml(tmp_path, f"quality_gates:\n{body}")
        config = tmp_path / ".claude" / "project-config.yaml"
        assert _mod.load_timeout_overrides(config) == {"tests": 300}


class TestPrCiSnapshot:
    def _run(
        self, monkeypatch: pytest.MonkeyPatch, outcome: object
    ) -> tuple[dict[str, Any] | None, list[list[str]]]:
        seen: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            seen.append(cmd)
            if isinstance(outcome, BaseException):
                raise outcome
            return subprocess.CompletedProcess(cmd, 0, stdout=outcome, stderr="")

        monkeypatch.setattr(_mod.subprocess, "run", fake_run)
        return _mod._pr_ci_snapshot(7, "owner/name"), seen

    def test_parses_json_and_passes_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = json.dumps(_snapshot(_check_run(CHECK)))
        result, seen = self._run(monkeypatch, payload)
        assert result == _snapshot(_check_run(CHECK))
        assert seen[0][:4] == ["gh", "pr", "view", "7"]
        assert seen[0][-2:] == ["--repo", "owner/name"]

    def test_no_repo_flag_when_repo_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        monkeypatch.setattr(_mod.subprocess, "run", fake_run)
        _mod._pr_ci_snapshot(7, None)
        assert "--repo" not in seen[0]

    @pytest.mark.parametrize(
        "outcome",
        [
            subprocess.CalledProcessError(1, "gh"),
            subprocess.TimeoutExpired("gh", 60),
            FileNotFoundError("gh"),
            "not json",
            "[1, 2]",
        ],
        ids=["gh-error", "timeout", "gh-missing", "bad-json", "non-object"],
    )
    def test_any_failure_is_none(
        self, monkeypatch: pytest.MonkeyPatch, outcome: object
    ) -> None:
        result, _ = self._run(monkeypatch, outcome)
        assert result is None


class TestMainDispatch:
    def test_cli_prints_json_and_forwards_pr_and_repo(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        del project
        spy = _SnapshotSpy(_snapshot(_check_run(CHECK)))
        monkeypatch.setattr(_mod, "_pr_ci_snapshot", spy)
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
            ],
        )
        _mod.main()
        out = json.loads(capsys.readouterr().out)
        assert out["skip"] is True
        assert spy.calls == [(7, "o/r")]

    def test_cli_exits_zero_with_skip_false_on_miss(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        del project
        monkeypatch.setattr(_mod, "_pr_ci_snapshot", _SnapshotSpy(None))
        monkeypatch.setattr(
            sys, "argv", ["prep_pr_state.py", "gate-ci-evidence", GATE, "--pr", "7"]
        )
        _mod.main()
        out = json.loads(capsys.readouterr().out)
        assert out["skip"] is False
        assert out["reason"] == "ci-signal-unavailable"

    def test_cli_requires_pr(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del project
        monkeypatch.setattr(sys, "argv", ["prep_pr_state.py", "gate-ci-evidence", GATE])
        with pytest.raises(SystemExit) as excinfo:
            _mod.main()
        assert excinfo.value.code != 0
