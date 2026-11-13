"""Tests for cw.codex_finalize._ci_evidence: the codex CI-evidence consult (#3618).

The decision lives only in ``prep_pr_state.py`` (``ci-deferral`` and
``ci-reuse-plan``); the wrapper shells that script and consumes its JSON. The
unit classes drive the wrapper against the :class:`PrepPrStateStub` under the
per-test HOME and the shared ``prep_pr_finalize.py`` stub; the parity class
runs the REAL ``prep_pr_state.py`` subprocess behind the shared ``stub_gh_ci``
shim and asserts the wrapper agrees with the CLI's own ``park``/``skip``/``run``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from unittest.mock import patch

import pytest

from cw.codex_finalize._ci_evidence import (
    CiContext,
    DeferredGate,
    ReusePark,
    ReusePlan,
    awaiting_ci_evidence_block,
    ci_pr_info,
    consult_create,
    consult_reuse,
    release_ci_hold,
)
from cw.reconcile.review_recipes.repair_git import RepairStopError
from cw.repair_disposition import RepairDisposition
from tests._codex_review_helpers import PrepPrStateStub, install_home_prep_pr_state
from tests._shared.ci_evidence_fakes import CHECK, GATE, check_run, commit_status, pr
from tests._shared.git_repos import add_bare_origin, git_in
from tests._tool_fakes import prepend_to_path, stub_gh_ci, stub_prep_pr_finalize
from tests.test_prep_pr_state import _SCRIPT

if TYPE_CHECKING:
    from collections.abc import Callable

_HEAD = "c" * 40
_URL = "https://github.com/o/r/pull/7"
_REUSE = CiContext(base="main", pr_number=7, pr_url=_URL, head=_HEAD)
_CREATE = CiContext(base="main", head=_HEAD)
_RUN_URL = "https://github.example/o/r/runs/1"


@pytest.fixture
def stub(tmp_path: Path, tmp_config_dir: Path) -> PrepPrStateStub:
    del tmp_config_dir
    return install_home_prep_pr_state(
        tmp_path / "state", ("lint", "true", 5.0), ("fmt", "true", 5.0)
    )


def _park(flavor: str, state: str, url: str | None = None) -> dict[str, object]:
    return {
        "flavor": flavor,
        "gate": "lint",
        "checks": ["ci-lint"],
        "failed_check": "ci-lint",
        "check_state": state,
        "failed_url": url,
        "sha": _HEAD,
    }


# --- consult_create -----------------------------------------------------------


class TestConsultCreate:
    def test_a_mapped_gate_is_deferred_with_the_shared_friction_literal(
        self, stub: PrepPrStateStub, tmp_path: Path
    ) -> None:
        stub.defer(("lint", ("ci-lint", "ci-lint-2")))
        deferred, friction = consult_create(stub.script, tmp_path, "main")
        assert deferred == (DeferredGate("lint", ("ci-lint", "ci-lint-2")),)
        assert friction == ["create_path_gate_ci_deferred: lint via ci-lint, ci-lint-2"]
        assert stub.calls() == ["ci-deferral --base main"]

    def test_no_deferral_is_empty_and_silent(
        self, stub: PrepPrStateStub, tmp_path: Path
    ) -> None:
        assert consult_create(stub.script, tmp_path, "main") == ((), [])

    @pytest.mark.parametrize(
        ("raw", "cause"),
        [
            ("not json", "output is not JSON"),
            ("[]", "output is not a JSON object"),
            ('{"local": []}', "output has no deferred list"),
            ('{"deferred": "lint"}', "output has no deferred list"),
            ('{"deferred": ["lint"]}', "output has a malformed deferred entry"),
            (
                '{"deferred": [{"gate": "lint", "checks": []}]}',
                "output has a malformed deferred entry",
            ),
            (
                '{"deferred": [{"gate": "lint", "checks": [3]}]}',
                "output has a malformed deferred entry",
            ),
            (
                '{"deferred": [{"gate": 4, "checks": ["x"]}]}',
                "output has a malformed deferred entry",
            ),
        ],
    )
    def test_bad_output_is_unavailable_not_deferred(
        self, stub: PrepPrStateStub, tmp_path: Path, raw: str, cause: str
    ) -> None:
        stub.set(deferral_raw=raw)
        deferred, friction = consult_create(stub.script, tmp_path, "main")
        assert deferred == ()
        assert friction == [f"ci_evidence_unavailable: ci-deferral {cause}"]

    def test_a_non_zero_exit_is_unavailable_with_its_code(
        self, stub: PrepPrStateStub, tmp_path: Path
    ) -> None:
        stub.set(deferral_exit=3)
        deferred, friction = consult_create(stub.script, tmp_path, "main")
        assert deferred == ()
        assert friction == ["ci_evidence_unavailable: ci-deferral exit 3"]

    @pytest.mark.parametrize(
        ("error", "cause"),
        [
            (subprocess.TimeoutExpired("x", 1), "timed out"),
            (OSError("boom"), "could not start"),
        ],
    )
    def test_a_timeout_or_launch_failure_is_unavailable(
        self, stub: PrepPrStateStub, tmp_path: Path, error: BaseException, cause: str
    ) -> None:
        with patch("cw.codex_finalize._ci_evidence.subprocess.run", side_effect=error):
            deferred, friction = consult_create(stub.script, tmp_path, "main")
        assert deferred == ()
        assert friction == [f"ci_evidence_unavailable: ci-deferral {cause}"]

    def test_the_helper_is_run_in_the_worktree_with_a_bounded_timeout(
        self, stub: PrepPrStateStub, tmp_path: Path
    ) -> None:
        done = subprocess.CompletedProcess([], 0, '{"deferred": []}', "")
        with patch(
            "cw.codex_finalize._ci_evidence.subprocess.run", return_value=done
        ) as run:
            consult_create(stub.script, tmp_path, "trunk")
        assert run.call_args.args[0] == [
            sys.executable,
            str(stub.script),
            "ci-deferral",
            "--base",
            "trunk",
        ]
        assert run.call_args.kwargs["cwd"] == tmp_path
        assert run.call_args.kwargs["timeout"] == 300
        assert run.call_args.kwargs["check"] is False


class TestUnavailableBestEffortRelease:
    """Adopted Assumption 17(b): an abandoned deferral clears any stale hold."""

    def test_an_unavailable_consult_releases_a_possible_stale_hold(
        self, stub: PrepPrStateStub, tmp_path: Path
    ) -> None:
        stub.set(deferral_exit=1)
        finalize = stub_prep_pr_finalize(Path.home())
        _, friction = consult_create(stub.script, tmp_path, "main")
        assert friction == ["ci_evidence_unavailable: ci-deferral exit 1"]
        assert finalize.calls() == [
            ["ci-hold", "release", "--repo-path", str(tmp_path)]
        ]

    def test_a_failing_release_is_friction_never_a_block(
        self, stub: PrepPrStateStub, tmp_path: Path
    ) -> None:
        stub.set(deferral_exit=1)
        stub_prep_pr_finalize(Path.home(), hold_exit=1, hold_stderr="boom")
        deferred, friction = consult_create(stub.script, tmp_path, "main")
        assert deferred == ()
        assert friction == [
            "ci_evidence_unavailable: ci-deferral exit 1",
            "ci_evidence_unavailable: ci-hold release exit 1: boom",
        ]

    def test_a_missing_arm_script_has_nothing_to_release(
        self, stub: PrepPrStateStub, tmp_path: Path
    ) -> None:
        stub.set(deferral_exit=1)
        _, friction = consult_create(stub.script, tmp_path, "main")
        assert friction == ["ci_evidence_unavailable: ci-deferral exit 1"]

    def test_a_script_without_ci_hold_is_not_asked_to_release(
        self, stub: PrepPrStateStub, tmp_path: Path
    ) -> None:
        stub.set(deferral_exit=1)
        old = stub_prep_pr_finalize(Path.home())
        old.script.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
        _, friction = consult_create(stub.script, tmp_path, "main")
        assert friction == ["ci_evidence_unavailable: ci-deferral exit 1"]
        assert old.calls() == []

    def test_a_successful_consult_never_touches_the_hold(
        self, stub: PrepPrStateStub, tmp_path: Path
    ) -> None:
        stub.defer(("lint", ("ci-lint",)))
        finalize = stub_prep_pr_finalize(Path.home())
        consult_create(stub.script, tmp_path, "main")
        assert finalize.calls() == []


# --- consult_reuse ------------------------------------------------------------


class TestConsultReuse:
    def test_green_gates_are_skipped_with_the_existing_literal(
        self, stub: PrepPrStateStub, tmp_path: Path
    ) -> None:
        stub.reuse_plan(skip=(("lint", ("ci-lint", "ci-b")),))
        plan = consult_reuse(stub.script, tmp_path, _REUSE)
        assert plan == ReusePlan(
            park=None,
            skip_names=frozenset({"lint"}),
            friction=("reuse_path_gate_ci_covered: lint via ci-lint, ci-b",),
        )
        assert stub.calls() == ["ci-reuse-plan --pr 7 --base main"]

    def test_the_default_answer_runs_everything(
        self, stub: PrepPrStateStub, tmp_path: Path
    ) -> None:
        assert consult_reuse(stub.script, tmp_path, _REUSE) == ReusePlan()

    @pytest.mark.parametrize(
        ("flavor", "state", "url"),
        [
            ("pending", "missing", None),
            ("pending", "queued", None),
            ("pending", "in_progress", None),
            ("red", "failure", _RUN_URL),
            ("red", "timed_out", None),
        ],
    )
    def test_a_park_carries_every_field(
        self,
        stub: PrepPrStateStub,
        tmp_path: Path,
        flavor: str,
        state: str,
        url: str | None,
    ) -> None:
        stub.reuse_plan(park=_park(flavor, state, url))
        plan = consult_reuse(stub.script, tmp_path, _REUSE)
        assert plan.park == ReusePark(
            flavor=cast("Literal['pending', 'red']", flavor),
            gate="lint",
            checks=("ci-lint",),
            failed_check="ci-lint",
            check_state=state,
            failed_url=url,
            sha=_HEAD,
        )
        assert plan.skip_names == frozenset()

    @pytest.mark.parametrize(
        ("raw", "cause"),
        [
            ("not json", "output is not JSON"),
            ("[]", "output is not a JSON object"),
            ('{"skip": []}', "output has no park key"),
            ('{"park": null}', "output has no skip list"),
            ('{"park": null, "skip": {}}', "output has no skip list"),
            (
                '{"park": null, "skip": [{"gate": 1}]}',
                "output has a malformed skip entry",
            ),
            ('{"park": 3, "skip": []}', "output has a malformed park"),
            (
                json.dumps(
                    {"park": {**_park("pending", "x"), "flavor": "green"}, "skip": []}
                ),
                "output has a malformed park",
            ),
            (
                json.dumps({"park": {**_park("red", "x"), "sha": 4}, "skip": []}),
                "output has a malformed park",
            ),
            (
                json.dumps({"park": {**_park("red", "x"), "checks": [1]}, "skip": []}),
                "output has a malformed park",
            ),
        ],
    )
    def test_a_stale_or_bad_plan_runs_locally_and_says_so(
        self, stub: PrepPrStateStub, tmp_path: Path, raw: str, cause: str
    ) -> None:
        stub.set(reuse_plan_raw=raw)
        plan = consult_reuse(stub.script, tmp_path, _REUSE)
        assert plan.park is None
        assert plan.skip_names == frozenset()
        assert plan.friction == (f"ci_evidence_unavailable: ci-reuse-plan {cause}",)

    def test_a_non_zero_exit_is_unavailable(
        self, stub: PrepPrStateStub, tmp_path: Path
    ) -> None:
        stub.set(reuse_plan_exit=2)
        plan = consult_reuse(stub.script, tmp_path, _REUSE)
        assert plan == ReusePlan(
            friction=("ci_evidence_unavailable: ci-reuse-plan exit 2",)
        )

    def test_a_timeout_is_unavailable_never_a_park(
        self, stub: PrepPrStateStub, tmp_path: Path
    ) -> None:
        with patch(
            "cw.codex_finalize._ci_evidence.subprocess.run",
            side_effect=subprocess.TimeoutExpired("x", 300),
        ):
            plan = consult_reuse(stub.script, tmp_path, _REUSE)
        assert plan == ReusePlan(
            friction=("ci_evidence_unavailable: ci-reuse-plan timed out",)
        )

    def test_no_pr_number_never_calls_the_helper(
        self, stub: PrepPrStateStub, tmp_path: Path
    ) -> None:
        plan = consult_reuse(stub.script, tmp_path, _CREATE)
        assert plan == ReusePlan(
            friction=("ci_evidence_unavailable: ci-reuse-plan no PR number",)
        )
        assert stub.calls() == []


# --- awaiting_ci_evidence_block -----------------------------------------------


class TestBlock:
    def test_deferred_lists_every_gate_and_is_retryable(self) -> None:
        block = awaiting_ci_evidence_block(
            "deferred",
            _REUSE,
            deferred=(
                DeferredGate("lint", ("a", "b")),
                DeferredGate("types", ("c",)),
            ),
        )
        assert block.reason == "awaiting_ci_evidence"
        assert block.retry_eligible is True
        assert block.stage == "stage5_post_create"
        assert block.details == (
            f"ci_evidence_deferred: PR #7 ({_URL}) at head {_HEAD}: gate lint "
            "mapped to check(s) a, b deferred to CI and not run locally; gate "
            "types mapped to check(s) c deferred to CI and not run locally. "
            "cw did not arm auto-merge."
        )
        assert block.pr_info == {
            "number": 7,
            "url": _URL,
            "auto_merge": False,
            "base": "main",
        }

    def test_a_single_deferred_gate_matches_the_documented_text(self) -> None:
        block = awaiting_ci_evidence_block(
            "deferred", _REUSE, deferred=(DeferredGate("lint", ("a",)),)
        )
        assert block.details == (
            f"ci_evidence_deferred: PR #7 ({_URL}) at head {_HEAD}: gate lint "
            "mapped to check(s) a deferred to CI and not run locally. "
            "cw did not arm auto-merge."
        )

    def test_pending_names_the_deciding_check_and_state(self) -> None:
        park = ReusePark(
            "pending", "lint", ("ci-lint",), "ci-lint", "queued", None, _HEAD
        )
        block = awaiting_ci_evidence_block("pending", _REUSE, park=park)
        assert block.retry_eligible is True
        assert block.details == (
            f"ci_evidence_pending: PR #7 ({_URL}) at head {_HEAD}: gate lint "
            "mapped to check ci-lint is queued; cw did not arm auto-merge and "
            "did not run the gate locally."
        )

    def test_red_is_not_retry_eligible_and_says_no_url_when_absent(self) -> None:
        park = ReusePark(
            "red", "lint", ("ci-lint",), "ci-lint", "timed_out", None, _HEAD
        )
        block = awaiting_ci_evidence_block("red", _REUSE, park=park)
        assert block.retry_eligible is False
        assert block.details == (
            f"ci_evidence_red: PR #7 ({_URL}) at head {_HEAD}: gate lint mapped "
            "to check ci-lint concluded timed_out (no url). This is a failing CI "
            "check, not a gate failure; cw did not arm auto-merge and did not "
            "run the gate locally."
        )

    def test_hold_error_names_the_action_and_cause(self) -> None:
        block = awaiting_ci_evidence_block("hold_error", _REUSE, cause="exit 1: boom")
        assert block.retry_eligible is True
        assert block.details == (
            f"ci_evidence_hold_error: PR #7 ({_URL}) at head {_HEAD}: ci-hold "
            "release failed (exit 1: boom). cw did not arm auto-merge."
        )

    @pytest.mark.parametrize("flavor", ["pending", "red"])
    def test_a_park_flavor_without_its_park_is_a_programming_error(
        self, flavor: Literal["pending", "red"]
    ) -> None:
        with pytest.raises(ValueError, match="needs its ReusePark"):
            awaiting_ci_evidence_block(flavor, _REUSE)

    def test_the_pr_info_is_the_gate_timeout_shape(self) -> None:
        assert ci_pr_info(_REUSE) == {
            "number": 7,
            "url": _URL,
            "auto_merge": False,
            "base": "main",
        }


# --- release_ci_hold ----------------------------------------------------------


class TestReleaseCiHold:
    def _info(self) -> dict[str, object]:
        return ci_pr_info(_REUSE)

    def test_the_shared_stub_carries_ci_hold_and_is_released(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        del tmp_config_dir
        finalize = stub_prep_pr_finalize(Path.home())
        assert release_ci_hold(tmp_path, self._info(), head=_HEAD) is None
        assert finalize.calls() == [
            ["ci-hold", "release", "--repo-path", str(tmp_path)]
        ]

    def test_a_script_lacking_ci_hold_is_skipped(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        del tmp_config_dir
        finalize = stub_prep_pr_finalize(Path.home())
        finalize.script.write_text(
            "import sys\nsys.exit(2)  # an older prep_pr_finalize\n", encoding="utf-8"
        )
        with patch("cw.codex_finalize._ci_evidence.subprocess.run") as run:
            assert release_ci_hold(tmp_path, self._info(), head=_HEAD) is None
        run.assert_not_called()

    def test_a_non_zero_release_is_a_hold_error_block(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        del tmp_config_dir
        stub_prep_pr_finalize(Path.home(), hold_exit=1, hold_stderr="cannot remove")
        block = release_ci_hold(tmp_path, self._info(), head=_HEAD)
        assert block is not None
        assert block.reason == "awaiting_ci_evidence"
        assert block.retry_eligible is True
        assert block.stage == "stage5_post_create"
        assert block.details == (
            f"ci_evidence_hold_error: PR #7 ({_URL}) at head {_HEAD}: ci-hold "
            "release failed (exit 1: cannot remove). cw did not arm auto-merge."
        )
        assert block.pr_info is not None
        assert block.pr_info["auto_merge"] is False

    def test_a_missing_script_is_a_hold_error_never_a_silent_skip(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        del tmp_config_dir
        block = release_ci_hold(tmp_path, self._info(), head=_HEAD)
        assert block is not None
        assert block.details.startswith(
            f"ci_evidence_hold_error: PR #7 ({_URL}) at head {_HEAD}: ci-hold release "
            "failed ("
        )

    def test_a_repair_stop_from_the_probe_is_a_hold_error(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        del tmp_config_dir
        error = RepairStopError(
            RepairDisposition.ARM_SCRIPT_MISSING, "no script anywhere"
        )
        with patch(
            "cw.codex_finalize._ci_evidence._probe_arm_script", side_effect=error
        ):
            block = release_ci_hold(tmp_path, self._info(), head=_HEAD)
        assert block is not None
        assert "failed (no script anywhere)" in block.details

    @pytest.mark.parametrize(
        ("error", "cause"),
        [
            (subprocess.TimeoutExpired("x", 1), "timed out"),
            (OSError("boom"), "could not start"),
        ],
    )
    def test_a_timeout_or_launch_failure_is_a_hold_error(
        self,
        tmp_path: Path,
        tmp_config_dir: Path,
        error: BaseException,
        cause: str,
    ) -> None:
        del tmp_config_dir
        stub_prep_pr_finalize(Path.home())
        with patch("cw.codex_finalize._ci_evidence.subprocess.run", side_effect=error):
            block = release_ci_hold(tmp_path, self._info(), head=_HEAD)
        assert block is not None
        assert f"release failed ({cause})" in block.details

    def test_an_unreadable_script_is_skipped_like_one_without_ci_hold(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        del tmp_config_dir
        stub_prep_pr_finalize(Path.home())
        with patch.object(Path, "read_text", side_effect=OSError("denied")):
            assert release_ci_hold(tmp_path, self._info(), head=_HEAD) is None

    def test_an_unknown_head_is_named_unknown(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        del tmp_config_dir
        stub_prep_pr_finalize(Path.home(), hold_exit=1)
        block = release_ci_hold(tmp_path, self._info())
        assert block is not None
        assert "at head unknown:" in block.details


# --- parity: the wrapper against the real prep_pr_state.py ----------------------

_CONFIG = f"""\
quality_gates:
  ci_evidence:
    {GATE}: {CHECK}
"""
_GATES = (GATE, "ruff")


def _project(make_git_repo: Callable[..., Path], config: str | None) -> Path:
    """A repo with gates ``unit-tests`` and ``ruff``; *config* is on ``origin/main``."""
    repo = make_git_repo("parity")
    (repo / "CLAUDE.md").write_text(
        f"## Quality Gates\n\n- {GATE}: true\n- ruff: true\n", encoding="utf-8"
    )
    if config is not None:
        target = repo / ".claude" / "project-config.yaml"
        target.parent.mkdir()
        target.write_text(config, encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", "project")
    add_bare_origin(repo)
    git_in(repo, "checkout", "-b", "feature")
    return repo


def _cli(repo: Path, *argv: str) -> dict[str, Any]:
    done = subprocess.run(
        [sys.executable, str(_SCRIPT), *argv],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    parsed: dict[str, Any] = json.loads(done.stdout)
    return parsed


class TestParityWithTheRealCli:
    """Both backends decide through the one CLI: the wrapper adds no logic."""

    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            (None, {}),
            ("quality_gates: [unclosed", {}),
            ("quality_gates:\n  ci_evidence:\n    other-gate: x\n", {}),
            (_CONFIG, {GATE: (CHECK,)}),
        ],
        ids=["no-config", "malformed", "unmapped", "mapped"],
    )
    def test_create_defers_exactly_what_the_cli_defers(
        self,
        make_git_repo: Callable[..., Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        config: str | None,
        expected: dict[str, tuple[str, ...]],
    ) -> None:
        repo = _project(make_git_repo, config)
        bin_dir = stub_gh_ci(tmp_path, pr=None, check_runs=None, statuses=None)
        prepend_to_path(monkeypatch, bin_dir)
        cli = _cli(repo, "ci-deferral", "--base", "main")
        deferred, friction = consult_create(_SCRIPT, repo, "main")
        assert {d.gate: d.checks for d in deferred} == expected
        assert [{"gate": d.gate, "checks": list(d.checks)} for d in deferred] == cli[
            "deferred"
        ]
        assert [g for g in _GATES if g not in expected] == cli["local"]
        assert friction == [
            f"create_path_gate_ci_deferred: {gate} via {', '.join(checks)}"
            for gate, checks in expected.items()
        ]

    @pytest.mark.parametrize(
        ("runs", "statuses", "flavor", "state"),
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
            ([], [commit_status(CHECK, "error")], "red", "error"),
            ([check_run(CHECK, conclusion="cancelled")], [], "local", None),
            ([check_run(CHECK, conclusion="neutral")], [], "local", None),
        ],
        ids=[
            "green",
            "pending",
            "missing",
            "red",
            "red-status",
            "cancelled",
            "neutral",
        ],
    )
    def test_reuse_decides_exactly_as_the_cli_does(
        self,
        make_git_repo: Callable[..., Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        runs: list[dict[str, Any]],
        statuses: list[dict[str, Any]],
        flavor: str | None,
        state: str | None,
    ) -> None:
        repo = _project(make_git_repo, _CONFIG)
        head = git_in(repo, "rev-parse", "HEAD")
        bin_dir = stub_gh_ci(
            tmp_path, pr=pr(head=head), check_runs=runs, statuses=statuses
        )
        prepend_to_path(monkeypatch, bin_dir)
        ctx = CiContext(base="main", pr_number=7, pr_url=_URL, head=head)
        cli = _cli(repo, "ci-reuse-plan", "--pr", "7", "--base", "main")
        plan = consult_reuse(_SCRIPT, repo, ctx)
        if flavor in ("pending", "red"):
            assert cli["park"]["flavor"] == flavor
            assert plan.park is not None
            assert (plan.park.flavor, plan.park.check_state) == (flavor, state)
            assert plan.park.gate == cli["park"]["gate"]
            assert plan.park.sha == cli["park"]["sha"] == head
            assert plan.skip_names == frozenset()
            return
        assert cli["park"] is None
        assert plan.park is None
        assert plan.skip_names == {entry["gate"] for entry in cli["skip"]}
        assert set(_GATES) - plan.skip_names == set(cli["run"])
        assert plan.friction == tuple(
            f"reuse_path_gate_ci_covered: {e['gate']} via {', '.join(e['checks'])}"
            for e in cli["skip"]
        )

    def test_an_unreachable_github_runs_everything_in_both(
        self,
        make_git_repo: Callable[..., Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = _project(make_git_repo, _CONFIG)
        bin_dir = stub_gh_ci(tmp_path, pr=None, check_runs=None, statuses=None)
        prepend_to_path(monkeypatch, bin_dir)
        cli = _cli(repo, "ci-reuse-plan", "--pr", "7", "--base", "main")
        plan = consult_reuse(_SCRIPT, repo, _REUSE)
        assert cli == {"park": None, "skip": [], "run": [GATE, "ruff"]}
        assert plan.park is None
        assert plan.skip_names == frozenset()
