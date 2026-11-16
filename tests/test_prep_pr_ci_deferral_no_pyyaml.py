"""The real ``prep_pr_state.py`` under ``python -S``: PyYAML is not importable (#3673).

A declared ``quality_gates.ci_evidence`` key must fail loud on every subcommand
(a park, exit 1, or a stderr warning), while an undeclared key keeps today's
quiet local-run behaviour. The shared harness lives in
``tests/test_prep_pr_ci_deferral.py``; this module only picks ``_NO_SITE``.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from tests._shared.ci_evidence_fakes import CONFIG, GATE
from tests._tool_fakes import gh_calls, stub_gh_ci
from tests.test_prep_pr_ci_deferral import _project_repo, _ShimHarness
from tests.test_prep_pr_finalize import _python

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_BOTH_TOKENS = ("pyyaml", "pyyaml-unavailable")


def _tokens_in(text: str) -> bool:
    lowered = text.lower()
    return all(token in lowered for token in _BOTH_TOKENS)


class TestRealCliWithoutPyYaml(_ShimHarness):
    _NO_SITE = True

    def test_dash_s_hides_pyyaml(self) -> None:
        done = subprocess.run(
            [*_python(no_site=True), "-c", "import yaml"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode != 0
        assert "No module named 'yaml'" in done.stderr

    def test_declared_key_parks_hold_error(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _project_repo(make_git_repo, origin_config=CONFIG, branch_config=CONFIG)
        bin_dir = stub_gh_ci(tmp_path, pr=None, check_runs=None, statuses=None)
        done = self._run(repo, bin_dir, "ci-reuse-plan", "--pr", "7", "--base", "main")
        assert done.returncode == 0, done.stderr
        plan = json.loads(done.stdout)
        assert plan["park"]["flavor"] == "hold_error"
        assert plan["park"]["cause"] == "pyyaml-unavailable"
        assert plan["park"]["sha"] == self._head(repo)
        assert (plan["skip"], plan["run"]) == ([], [])
        assert gh_calls(bin_dir) == []

    def test_declared_key_makes_ci_deferral_exit_one(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _project_repo(make_git_repo, origin_config=CONFIG, branch_config=CONFIG)
        bin_dir = stub_gh_ci(tmp_path, pr=None, check_runs=None, statuses=None)
        done = self._run(repo, bin_dir, "ci-deferral", "--base", "main")
        assert done.returncode == 1
        assert done.stdout == ""
        assert _tokens_in(done.stderr)

    def test_declared_key_warns_on_gate_ci_evidence(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _project_repo(make_git_repo, origin_config=CONFIG, branch_config=CONFIG)
        bin_dir = stub_gh_ci(tmp_path, pr=None, check_runs=None, statuses=None)
        done = self._run(
            repo, bin_dir, "gate-ci-evidence", GATE, "--pr", "7", "--base", "main"
        )
        assert done.returncode == 0, done.stderr
        verdict = json.loads(done.stdout)
        assert verdict["reason"] == "pyyaml-unavailable"
        assert verdict["outcome"] == "local"
        assert _tokens_in(done.stderr)

    @pytest.mark.parametrize(
        "origin_config",
        ["", "quality_gates:\n  ci_evidence: {}\n"],
        ids=["absent", "empty-map"],
    )
    def test_undeclared_key_stays_quiet_and_runs_locally(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        origin_config: str,
    ) -> None:
        repo = _project_repo(
            make_git_repo, origin_config=origin_config, branch_config=origin_config
        )
        bin_dir = stub_gh_ci(tmp_path, pr=None, check_runs=None, statuses=None)

        plan = self._run(repo, bin_dir, "ci-reuse-plan", "--pr", "7", "--base", "main")
        assert plan.returncode == 0
        assert json.loads(plan.stdout) == {
            "park": None,
            "skip": [],
            "run": [GATE, "ruff"],
        }

        deferral = self._run(repo, bin_dir, "ci-deferral", "--base", "main")
        assert deferral.returncode == 0
        assert json.loads(deferral.stdout)["reason"] == "pyyaml-unavailable"

        gate = self._run(
            repo, bin_dir, "gate-ci-evidence", GATE, "--pr", "7", "--base", "main"
        )
        assert gate.returncode == 0
        assert "pyyaml-unavailable" not in gate.stderr.lower()
