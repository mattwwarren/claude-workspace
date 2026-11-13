"""Tests for ``prep_pr_state.py ci-deferral`` and ``ci-reuse-plan`` (#3618).

Both subcommands compose ``detect-gates`` (the gate names, in detect order)
with the base branch's committed ``quality_gates.ci_evidence`` map. They are
the one place the "defer to CI / skip as green / park / run locally" decision
is made; Claude prose and the codex wrapper only consume their JSON.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from tests._shared.ci_evidence_fakes import (
    CHECK,
    CONFIG,
    GATE,
    HEAD,
    Github,
    check_run,
    commit_status,
    config_returning,
    git_returning,
    pr,
)
from tests._shared.git_repos import add_bare_origin, git_in
from tests._tool_fakes import gh_calls, shim_env, stub_gh_ci
from tests.test_prep_pr_state import _SCRIPT, _mod

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

LINT = "lint"
LINT_CHECK = "lint-job"
_TWO_GATE_CONFIG = f"""\
quality_gates:
  ci_evidence:
    {GATE}: {CHECK}
    {LINT}: [{LINT_CHECK}, second]
"""
_RUN_URL = "https://github.example/o/r/runs/1"


def _gates(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    monkeypatch.setattr(_mod, "_gate_names", lambda: list(names))


def _config(monkeypatch: pytest.MonkeyPatch, text: str) -> list[str]:
    """Serve *text* as the committed config; return the bases it was asked for."""
    bases: list[str] = []
    answer = config_returning(text)

    def fake_config(base: str) -> str:
        bases.append(base)
        return answer(base)

    monkeypatch.setattr(_mod, "_base_config_text", fake_config)
    return bases


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
) -> dict[str, Any]:
    monkeypatch.setattr(sys, "argv", ["prep_pr_state.py", *argv])
    _mod.main()
    out: dict[str, Any] = json.loads(capsys.readouterr().out)
    return out


class TestGateNames:
    def test_names_follow_detect_gates_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        detected = {
            "gates": [
                {"name": "b", "command": "x"},
                {"name": "a", "command": "y", "autofix": "z"},
            ]
        }
        monkeypatch.setattr(_mod, "detect_gates", lambda: detected)
        assert _mod._gate_names() == ["b", "a"]

    def test_no_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_mod, "detect_gates", lambda: {"gates": []})
        assert _mod._gate_names() == []


class TestCiDeferral:
    def test_mapped_gate_is_deferred_and_the_rest_stay_local(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gates(monkeypatch, "ruff", GATE, "mypy")
        _config(monkeypatch, CONFIG)
        assert _mod.ci_deferral("main") == {
            "deferred": [{"gate": GATE, "checks": [CHECK]}],
            "local": ["ruff", "mypy"],
            "reason": "mapped",
        }

    def test_list_entry_keeps_every_check_in_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gates(monkeypatch, LINT)
        _config(monkeypatch, _TWO_GATE_CONFIG)
        result = _mod.ci_deferral("main")
        assert result["deferred"] == [{"gate": LINT, "checks": [LINT_CHECK, "second"]}]

    def test_deferred_and_local_follow_detect_order_and_are_disjoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gates(monkeypatch, LINT, "mypy", GATE, "ruff")
        _config(monkeypatch, _TWO_GATE_CONFIG)
        result = _mod.ci_deferral("main")
        assert [d["gate"] for d in result["deferred"]] == [LINT, GATE]
        assert result["local"] == ["mypy", "ruff"]
        assert not {d["gate"] for d in result["deferred"]} & set(result["local"])

    @pytest.mark.parametrize(
        "gate", ["Unit-Tests", "unit", "unit-tests ", "unit-tests-2"]
    )
    def test_only_an_exact_name_is_deferred(
        self, monkeypatch: pytest.MonkeyPatch, gate: str
    ) -> None:
        _gates(monkeypatch, gate)
        _config(monkeypatch, CONFIG)
        assert _mod.ci_deferral("main") == {
            "deferred": [],
            "local": [gate],
            "reason": "no-map-entry",
        }

    def test_map_without_a_matching_gate_is_no_map_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gates(monkeypatch, "ruff")
        _config(monkeypatch, CONFIG)
        assert _mod.ci_deferral("main")["reason"] == "no-map-entry"

    @pytest.mark.parametrize(
        "config",
        ["", "quality_gates: [unclosed\n", "- a\n- list\n", "quality_gates: x\n"],
        ids=["absent", "malformed", "non-mapping", "non-mapping-section"],
    )
    def test_unusable_map_defers_nothing(
        self, monkeypatch: pytest.MonkeyPatch, config: str
    ) -> None:
        _gates(monkeypatch, GATE, "ruff")
        _config(monkeypatch, config)
        assert _mod.ci_deferral("main") == {
            "deferred": [],
            "local": [GATE, "ruff"],
            "reason": "no-map-entry",
        }

    def test_without_pyyaml_the_reason_says_why(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gates(monkeypatch, GATE)
        _config(monkeypatch, CONFIG)
        monkeypatch.setattr(_mod, "yaml", None)
        assert _mod.ci_deferral("main") == {
            "deferred": [],
            "local": [GATE],
            "reason": "pyyaml-unavailable",
        }

    def test_no_gates_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _gates(monkeypatch)
        _config(monkeypatch, CONFIG)
        assert _mod.ci_deferral("main") == {
            "deferred": [],
            "local": [],
            "reason": "no-gates",
        }

    def test_pyyaml_unavailable_outranks_no_gates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gates(monkeypatch)
        monkeypatch.setattr(_mod, "yaml", None)
        assert _mod.ci_deferral("main")["reason"] == "pyyaml-unavailable"

    def test_never_calls_github(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _gates(monkeypatch, GATE)
        _config(monkeypatch, CONFIG)

        def boom(*_args: object, **_kwargs: object) -> None:
            pytest.fail("ci-deferral must not reach GitHub")

        monkeypatch.setattr(_mod, "_pr_state", boom)
        monkeypatch.setattr(_mod, "_check_entries", boom)
        monkeypatch.setattr(_mod, "_gh_json", boom)
        assert _mod.ci_deferral("main")["reason"] == "mapped"


class TestCiDeferralCli:
    def test_prints_json_forwards_base_and_exits_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _gates(monkeypatch, GATE)
        bases = _config(monkeypatch, CONFIG)
        out = _run_cli(monkeypatch, capsys, "ci-deferral", "--base", "develop")
        assert out["reason"] == "mapped"
        assert bases == ["develop"]

    def test_base_defaults_to_main(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _gates(monkeypatch, GATE)
        bases = _config(monkeypatch, "")
        out = _run_cli(monkeypatch, capsys, "ci-deferral")
        assert out["reason"] == "no-map-entry"
        assert bases == ["main"]

    def test_exit_zero_even_when_nothing_can_be_read(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _gates(monkeypatch)
        _config(monkeypatch, "")
        monkeypatch.setattr(sys, "argv", ["prep_pr_state.py", "ci-deferral"])
        _mod.main()  # no SystemExit
        assert json.loads(capsys.readouterr().out)["reason"] == "no-gates"

    def test_help_string(self) -> None:
        done = subprocess.run(
            [sys.executable, str(_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        flat = " ".join(done.stdout.split())
        assert "ci-deferral" in flat
        assert "ci-reuse-plan" in flat


def _reuse_plan(
    monkeypatch: pytest.MonkeyPatch,
    github: Github,
    *,
    config: str = CONFIG,
    repo: str | None = None,
    base: str = "main",
) -> dict[str, Any]:
    monkeypatch.setattr(_mod, "_base_config_text", config_returning(config))
    monkeypatch.setattr(_mod, "_run_git", git_returning(HEAD))
    monkeypatch.setattr(_mod, "_pr_state", github.pr_state)
    monkeypatch.setattr(_mod, "_check_entries", github.check_entries)
    plan: dict[str, Any] = _mod.ci_reuse_plan(7, repo, base)
    return plan


def _green() -> dict[str, list[dict[str, Any]] | None]:
    return {CHECK: [check_run(CHECK)], LINT_CHECK: [check_run(LINT_CHECK)]}


class TestCiReusePlan:
    def test_green_gate_is_skipped_and_the_rest_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gates(monkeypatch, "ruff", GATE)
        plan = _reuse_plan(monkeypatch, Github(pr(), _green()))
        assert plan == {
            "park": None,
            "skip": [{"gate": GATE, "checks": [CHECK]}],
            "run": ["ruff"],
        }

    def test_zero_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _gates(monkeypatch)
        assert _reuse_plan(monkeypatch, Github(pr())) == {
            "park": None,
            "skip": [],
            "run": [],
        }

    def test_unmapped_gate_runs_and_never_reaches_github(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gates(monkeypatch, "ruff")
        github = Github(pr(), _green())
        plan = _reuse_plan(monkeypatch, github)
        assert plan == {"park": None, "skip": [], "run": ["ruff"]}
        assert github.pr_calls == []

    @pytest.mark.parametrize(
        ("entries", "flavor", "state"),
        [
            (
                [check_run(CHECK, status="in_progress", conclusion="")],
                "pending",
                "in_progress",
            ),
            ([check_run(CHECK, status="queued", conclusion="")], "pending", "queued"),
            ([commit_status(CHECK, "pending")], "pending", "pending"),
            ([], "pending", "missing"),
            (
                [check_run(CHECK, conclusion="failure", html_url=_RUN_URL)],
                "red",
                "failure",
            ),
            ([commit_status(CHECK, "error")], "red", "error"),
        ],
        ids=["in-progress", "queued", "status-pending", "missing", "failure", "error"],
    )
    def test_pending_missing_and_red_park_and_run_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        entries: list[dict[str, Any]],
        flavor: str,
        state: str,
    ) -> None:
        _gates(monkeypatch, "ruff", GATE)
        plan = _reuse_plan(monkeypatch, Github(pr(), {CHECK: entries}))
        assert plan["skip"] == []
        assert plan["run"] == []
        park = plan["park"]
        assert park["flavor"] == flavor
        assert park["gate"] == GATE
        assert park["checks"] == [CHECK]
        assert park["failed_check"] == CHECK
        assert park["check_state"] == state
        assert park["sha"] == HEAD

    def test_red_park_carries_the_failing_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gates(monkeypatch, GATE)
        run = check_run(CHECK, conclusion="failure", html_url=_RUN_URL)
        plan = _reuse_plan(monkeypatch, Github(pr(), {CHECK: [run]}))
        assert plan["park"]["failed_url"] == _RUN_URL

    def test_missing_park_has_no_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _gates(monkeypatch, GATE)
        plan = _reuse_plan(monkeypatch, Github(pr(), {CHECK: []}))
        assert plan["park"]["failed_url"] is None

    def test_red_beats_an_earlier_pending_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gates(monkeypatch, GATE, LINT)
        entries: dict[str, list[dict[str, Any]] | None] = {
            CHECK: [check_run(CHECK, status="in_progress", conclusion="")],
            LINT_CHECK: [check_run(LINT_CHECK, conclusion="failure")],
            "second": [check_run("second")],
        }
        plan = _reuse_plan(monkeypatch, Github(pr(), entries), config=_TWO_GATE_CONFIG)
        assert plan["park"]["flavor"] == "red"
        assert plan["park"]["gate"] == LINT
        assert plan["park"]["failed_check"] == LINT_CHECK

    def test_first_red_gate_in_detect_order_is_the_park(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gates(monkeypatch, LINT, GATE)
        entries: dict[str, list[dict[str, Any]] | None] = {
            CHECK: [check_run(CHECK, conclusion="failure")],
            LINT_CHECK: [check_run(LINT_CHECK, conclusion="failure")],
            "second": [check_run("second")],
        }
        plan = _reuse_plan(monkeypatch, Github(pr(), entries), config=_TWO_GATE_CONFIG)
        assert plan["park"]["gate"] == LINT

    def test_first_pending_gate_in_detect_order_is_the_park(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gates(monkeypatch, LINT, GATE)
        entries: dict[str, list[dict[str, Any]] | None] = {
            CHECK: [],
            LINT_CHECK: [check_run(LINT_CHECK, status="queued", conclusion="")],
            "second": [check_run("second")],
        }
        plan = _reuse_plan(monkeypatch, Github(pr(), entries), config=_TWO_GATE_CONFIG)
        assert plan["park"]["flavor"] == "pending"
        assert plan["park"]["gate"] == LINT

    def test_park_empties_skip_and_run_even_with_green_and_local_gates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gates(monkeypatch, "ruff", GATE, LINT)
        entries: dict[str, list[dict[str, Any]] | None] = {
            CHECK: [check_run(CHECK)],
            LINT_CHECK: [],
        }
        plan = _reuse_plan(monkeypatch, Github(pr(), entries), config=_TWO_GATE_CONFIG)
        assert plan["park"]["gate"] == LINT
        assert plan["skip"] == []
        assert plan["run"] == []

    @pytest.mark.parametrize(
        "conclusion", ["cancelled", "skipped", "neutral", "stale", "action_required"]
    )
    def test_inconclusive_check_runs_locally(
        self, monkeypatch: pytest.MonkeyPatch, conclusion: str
    ) -> None:
        _gates(monkeypatch, GATE)
        run = check_run(CHECK, conclusion=conclusion)
        plan = _reuse_plan(monkeypatch, Github(pr(), {CHECK: [run]}))
        assert plan == {"park": None, "skip": [], "run": [GATE]}

    @pytest.mark.parametrize(
        "github",
        [
            Github(None),
            Github(pr(state="MERGED")),
            Github(pr(base="release")),
            Github(pr(head="c" * 40)),
            Github(pr(), {CHECK: None}),
        ],
        ids=[
            "pr-unreadable",
            "pr-not-open",
            "base-mismatch",
            "head-mismatch",
            "no-signal",
        ],
    )
    def test_every_non_ci_reason_runs_locally(
        self, monkeypatch: pytest.MonkeyPatch, github: Github
    ) -> None:
        _gates(monkeypatch, GATE)
        assert _reuse_plan(monkeypatch, github) == {
            "park": None,
            "skip": [],
            "run": [GATE],
        }

    def test_unusable_map_runs_everything_locally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gates(monkeypatch, GATE, "ruff")
        plan = _reuse_plan(monkeypatch, Github(pr(), _green()), config="")
        assert plan == {"park": None, "skip": [], "run": [GATE, "ruff"]}

    def test_without_pyyaml_everything_runs_locally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gates(monkeypatch, GATE)
        monkeypatch.setattr(_mod, "yaml", None)
        plan = _reuse_plan(monkeypatch, Github(pr(), _green()))
        assert plan == {"park": None, "skip": [], "run": [GATE]}

    def test_repo_and_base_are_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _gates(monkeypatch, GATE)
        github = Github(pr(base="develop"), _green())
        plan = _reuse_plan(monkeypatch, github, repo="o/r", base="develop")
        assert plan["skip"] == [{"gate": GATE, "checks": [CHECK]}]
        assert github.pr_calls == [(7, "o/r")]

    def test_a_gate_missing_from_both_lists_is_not_a_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Consumers run a gate locally unless it is named in ``skip``.
        _gates(monkeypatch, GATE)
        plan = _reuse_plan(
            monkeypatch, Github(pr(), {CHECK: [commit_status(CHECK, "x")]})
        )
        assert [s["gate"] for s in plan["skip"]] == []


class TestCiReusePlanCli:
    def _argv(self, *extra: str) -> tuple[str, ...]:
        return ("ci-reuse-plan", "--pr", "7", *extra)

    def test_prints_json_and_forwards_pr_repo_and_base(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _gates(monkeypatch, GATE)
        bases = _config(monkeypatch, CONFIG)
        monkeypatch.setattr(_mod, "_run_git", git_returning(HEAD))
        github = Github(pr(base="develop"), _green())
        monkeypatch.setattr(_mod, "_pr_state", github.pr_state)
        monkeypatch.setattr(_mod, "_check_entries", github.check_entries)
        out = _run_cli(
            monkeypatch, capsys, *self._argv("--repo", "o/r", "--base", "develop")
        )
        assert out["skip"] == [{"gate": GATE, "checks": [CHECK]}]
        assert bases == ["develop"]
        assert github.pr_calls == [(7, "o/r")]

    def test_base_defaults_to_main_and_a_park_exits_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _gates(monkeypatch, GATE)
        bases = _config(monkeypatch, CONFIG)
        monkeypatch.setattr(_mod, "_run_git", git_returning(HEAD))
        github = Github(pr(), {CHECK: []})
        monkeypatch.setattr(_mod, "_pr_state", github.pr_state)
        monkeypatch.setattr(_mod, "_check_entries", github.check_entries)
        out = _run_cli(monkeypatch, capsys, *self._argv())
        assert out["park"]["flavor"] == "pending"
        assert bases == ["main"]

    def test_requires_pr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["prep_pr_state.py", "ci-reuse-plan"])
        with pytest.raises(SystemExit) as excinfo:
            _mod.main()
        assert excinfo.value.code == 2


def _project_repo(
    make_git_repo: Callable[..., Path],
    *,
    origin_config: str,
    branch_config: str,
) -> Path:
    """A repo whose ``origin/main`` carries *origin_config*; the branch *branch_config*.

    The branch's working-tree config is committed but never pushed, so the two
    differ exactly the way a PR that edits its own ``ci_evidence`` map does.
    """
    repo = make_git_repo("proj")
    (repo / "CLAUDE.md").write_text(
        f"## Quality Gates\n\n- {GATE}: true\n- ruff: true\n", encoding="utf-8"
    )
    config = repo / ".claude" / "project-config.yaml"
    config.parent.mkdir()
    config.write_text(origin_config, encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", "config")
    add_bare_origin(repo)
    git_in(repo, "checkout", "-b", "feature")
    config.write_text(branch_config, encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "--allow-empty", "-m", "edit own map")
    return repo


class TestOriginBaseIsTheOnlyMapSource:
    def _deferral(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        monkeypatch.chdir(repo)
        _gates(monkeypatch, GATE, "ruff")
        result: dict[str, Any] = _mod.ci_deferral("main")
        return result

    def test_a_map_only_in_the_working_tree_defers_nothing(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _project_repo(make_git_repo, origin_config="", branch_config=CONFIG)
        assert (repo / ".claude" / "project-config.yaml").read_text() == CONFIG
        assert self._deferral(repo, monkeypatch) == {
            "deferred": [],
            "local": [GATE, "ruff"],
            "reason": "no-map-entry",
        }

    def test_a_map_on_origin_base_defers_even_if_the_branch_drops_it(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _project_repo(make_git_repo, origin_config=CONFIG, branch_config="")
        result = self._deferral(repo, monkeypatch)
        assert result["deferred"] == [{"gate": GATE, "checks": [CHECK]}]
        assert result["reason"] == "mapped"


class TestRealCliThroughGhShim:
    """The real script as a subprocess, with ``gh`` replaced by ``stub_gh_ci``."""

    def _run(
        self,
        repo: Path,
        bin_dir: Path,
        *argv: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(_SCRIPT), *argv],
            cwd=repo,
            env=shim_env(bin_dir, HOME=str(repo)),
            capture_output=True,
            text=True,
            check=False,
        )

    def _head(self, repo: Path) -> str:
        return git_in(repo, "rev-parse", "HEAD")

    def test_ci_deferral_defers_the_mapped_gate(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _project_repo(make_git_repo, origin_config=CONFIG, branch_config=CONFIG)
        bin_dir = stub_gh_ci(tmp_path, pr=None, check_runs=None, statuses=None)
        done = self._run(repo, bin_dir, "ci-deferral", "--base", "main")
        assert done.returncode == 0
        assert json.loads(done.stdout) == {
            "deferred": [{"gate": GATE, "checks": [CHECK]}],
            "local": ["ruff"],
            "reason": "mapped",
        }
        assert gh_calls(bin_dir) == []

    @pytest.mark.parametrize(
        ("runs", "statuses", "park_flavor", "park_state"),
        [
            ([check_run(CHECK)], [], None, None),
            (
                [check_run(CHECK, status="in_progress", conclusion="")],
                [],
                "pending",
                "in_progress",
            ),
            ([], [], "pending", "missing"),
            (
                [check_run(CHECK, conclusion="failure", html_url=_RUN_URL)],
                [],
                "red",
                "failure",
            ),
            ([], [commit_status(CHECK, "failure")], "red", "failure"),
        ],
        ids=["green", "pending", "missing", "red", "red-status"],
    )
    def test_ci_reuse_plan_decides_from_the_stubbed_checks(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        runs: list[dict[str, Any]],
        statuses: list[dict[str, Any]],
        park_flavor: str | None,
        park_state: str | None,
    ) -> None:
        repo = _project_repo(make_git_repo, origin_config=CONFIG, branch_config=CONFIG)
        head = self._head(repo)
        bin_dir = stub_gh_ci(
            tmp_path,
            pr=pr(head=head),
            check_runs=runs,
            statuses=statuses,
        )
        done = self._run(repo, bin_dir, "ci-reuse-plan", "--pr", "7", "--base", "main")
        assert done.returncode == 0
        plan = json.loads(done.stdout)
        if park_flavor is None:
            assert plan == {
                "park": None,
                "skip": [{"gate": GATE, "checks": [CHECK]}],
                "run": ["ruff"],
            }
        else:
            assert plan["park"]["flavor"] == park_flavor
            assert plan["park"]["check_state"] == park_state
            assert plan["park"]["sha"] == head
            assert (plan["skip"], plan["run"]) == ([], [])
        assert gh_calls(bin_dir)

    def test_gh_unreachable_runs_everything_locally(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _project_repo(make_git_repo, origin_config=CONFIG, branch_config=CONFIG)
        bin_dir = stub_gh_ci(tmp_path, pr=None, check_runs=None, statuses=None)
        done = self._run(repo, bin_dir, "ci-reuse-plan", "--pr", "7", "--base", "main")
        assert done.returncode == 0
        assert json.loads(done.stdout) == {
            "park": None,
            "skip": [],
            "run": [GATE, "ruff"],
        }
