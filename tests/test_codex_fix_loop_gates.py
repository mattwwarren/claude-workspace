"""Post-fix quality gates in the codex fix loop (#3056, RFC 0018 A1).

Unit classes pin the cycle-start planner (:func:`plan_cycle_gates`), the
runner (:func:`run_cycle_gates`) and its signal-only slow-gate ceiling. The
wiring class drives the real loop over a real git repo, so a red, dirtied or
unavailable gate parks ``codex_fix_gate_failed`` through the same preserve and
reset path every other restorable breach takes, and the cycle-start pin (R22)
is proved by rewriting the detector and CLAUDE.md during the fix turn.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from cw.codex_fix_loop import commit, gates
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT
from cw.codex_fix_loop.gates import (
    GateFailure,
    GatePlan,
    GateScope,
    GateUnavailableError,
    PlannedGate,
    gate_failure_breach,
    plan_cycle_gates,
    resolve_prep_pr_state,
    run_cycle_gates,
)
from cw.codex_review import CODEX_FIX_GATE_FAILED
from cw.codex_runner import CodexRunResult
from cw.models.enums import OrchestratorEventType
from tests._codex_review_helpers import (
    _drive_loop,
    _FakeRunner,
    assert_clean_after_refusal,
)
from tests.conftest import add_bare_origin, commit_tracked_file, git_in
from tests.test_codex_fix_loop_convergence import _accepted
from tests.test_prep_pr_script_resolution import (
    PREP_PR_PATH,
    RESOLVER_ANCHOR,
    _fence_after,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.auto_dev_result import AutoDevResult

_LOGGER = "cw.codex_fix_loop.gates"
_SCOPE = GateScope(ticket_id="T-1", client="test", session_id="s-1", cycle=1)
_CEILING = 5.0
_SLOW_CEILING = 0.2
_SLOW_SLEEP = "0.7"
_INTERRUPT_TICK = 2
_MIN_OUTPUT_GAP_S = 0.3
_NONE_FRICTION = "codex_fix_gates_none_detected"
_STUB_TEMPLATE = '''\
"""prep_pr_state stub: understands gate-timeout."""
import json
import sys
import time

SIDE = __SIDE__
CALLS = __CALLS__
cfg = json.load(open(SIDE))
with open(CALLS, "a") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1] == "detect-gates":
    time.sleep(cfg["detect_delay"])
    raw = cfg["detect_raw"]
    print(raw if raw is not None else json.dumps(
        {"gates": cfg["gates"], "detected_from": []}))
    sys.exit(cfg["detect_exit"])
if sys.argv[1] == "gate-timeout":
    time.sleep(cfg["ceiling_delay"])
    raw = cfg["ceiling_raw"]
    print(raw if raw is not None else json.dumps(
        {"gate": sys.argv[2], "foreground_ceiling_s": cfg["ceilings"][sys.argv[2]]}))
    sys.exit(cfg["ceiling_exit"])
sys.exit(2)
'''


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gates, "_POLL_SECONDS", 0.05)


@pytest.fixture
def slow_events(capture_events: Callable[..., list[tuple]]) -> list[tuple]:
    return capture_events(_LOGGER, OrchestratorEventType.CODEX_FIX_GATE_SLOW)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    path.mkdir()
    return path


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> Path:
    path = make_git_repo("gates-wt")
    commit_tracked_file(path, "tracked.py", "x = 1\n")
    (path / ".gitignore").write_text(".coverage\n", encoding="utf-8")
    git_in(path, "add", ".gitignore")
    git_in(path, "commit", "-m", "ignore coverage")
    return path


class _Stub:
    """A fake ``prep_pr_state.py`` plus the call log it appends to."""

    def __init__(self, scripts_dir: Path, state: Path) -> None:
        self.script = scripts_dir / "prep_pr_state.py"
        self.calls_path = state / "calls.log"
        self.side = state / "stub.json"
        self.config: dict[str, object] = {
            "gates": [],
            "ceilings": {},
            "detect_delay": 0,
            "ceiling_delay": 0,
            "detect_raw": None,
            "detect_exit": 0,
            "ceiling_raw": None,
            "ceiling_exit": 0,
        }
        scripts_dir.mkdir(parents=True, exist_ok=True)
        text = _STUB_TEMPLATE.replace("__SIDE__", repr(str(self.side))).replace(
            "__CALLS__", repr(str(self.calls_path))
        )
        self.script.write_text(text, encoding="utf-8")
        self.set()

    def set(self, **overrides: object) -> None:
        self.config.update(overrides)
        self.side.write_text(json.dumps(self.config), encoding="utf-8")

    def gates(self, *entries: tuple[str, str, float]) -> None:
        self.set(
            gates=[{"name": n, "command": c} for n, c, _ in entries],
            ceilings={n: ceiling for n, _, ceiling in entries},
        )

    def calls(self) -> list[str]:
        if not self.calls_path.exists():
            return []
        return self.calls_path.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def home_stub(state: Path) -> _Stub:
    return _Stub(Path.home() / ".claude" / "scripts", state)


def _log_cmd(log: Path, name: str, body: str = "") -> str:
    return f"echo {name} >> {shlex.quote(str(log))}; {body}".rstrip("; ")


def _plan(*entries: tuple[str, str, float]) -> GatePlan:
    return GatePlan(
        script=None,
        gates=tuple(PlannedGate(n, c, ceiling) for n, c, ceiling in entries),
        why_unavailable=None,
    )


def _run(
    plan: GatePlan, repo: Path, tokens: list[str] | None = None
) -> GateFailure | None:
    return run_cycle_gates(
        plan,
        worktree=repo,
        review_artifacts=frozenset(),
        scope=_SCOPE,
        tokens=tokens if tokens is not None else [],
    )


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class TestResolver:
    def test_probe_order_first_hit_wins(self, repo: Path, state: Path) -> None:
        home = _Stub(Path.home() / ".claude" / "scripts", state)
        scripts = _Stub(repo / "scripts", state)
        claude = _Stub(repo / ".claude" / "scripts", state)

        assert resolve_prep_pr_state(repo) == claude.script
        claude.script.unlink()
        assert resolve_prep_pr_state(repo) == scripts.script
        scripts.script.unlink()
        assert resolve_prep_pr_state(repo) == home.script

    def test_stale_first_hit_is_not_skipped(self, repo: Path, state: Path) -> None:
        stale = repo / ".claude" / "scripts" / "prep_pr_state.py"
        stale.parent.mkdir(parents=True)
        stale.write_text("print('no timeout subcommand')\n", encoding="utf-8")
        _Stub(Path.home() / ".claude" / "scripts", state)

        with pytest.raises(GateUnavailableError, match="STALE"):
            resolve_prep_pr_state(repo)
        plan = plan_cycle_gates(repo, _SCOPE)
        assert plan.why_unavailable is not None
        assert "STALE" in plan.why_unavailable

    def test_missing_script_lists_probed_paths_and_starts_nothing(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_popen(*_a: object, **_k: object) -> None:
            raise AssertionError

        monkeypatch.setattr(subprocess, "Popen", _no_popen)

        plan = plan_cycle_gates(repo, _SCOPE)

        assert plan.script is None
        assert plan.why_unavailable is not None
        for probe in (".claude/scripts/", "scripts/", "~/.claude/scripts/"):
            assert probe in plan.why_unavailable

    def test_resolver_matches_the_prep_pr_fence(self) -> None:
        fence = _fence_after(PREP_PR_PATH, RESOLVER_ANCHOR)
        loop = re.search(r"for candidate in (.+?); do", fence)
        assert loop is not None
        prose = [c.strip('"') for c in loop.group(1).split()]
        assert prose == [
            *gates.REPO_SCRIPT_CANDIDATES,
            "$HOME/.claude/scripts/prep_pr_state.py",
        ]
        assert f"grep -q '{gates.STALE_MARKER}'" in fence


class TestPlanner:
    def test_happy_path_pins_gates_and_ceilings(
        self, repo: Path, home_stub: _Stub
    ) -> None:
        home_stub.gates(("lint", "ruff check .", 30), ("test", "pytest", 120.5))

        plan = plan_cycle_gates(repo, _SCOPE)

        assert plan.why_unavailable is None
        assert plan.script == home_stub.script
        assert plan.gates == (
            PlannedGate("lint", "ruff check .", 30),
            PlannedGate("test", "pytest", 120.5),
        )
        assert home_stub.calls() == [
            "detect-gates",
            "gate-timeout lint",
            "gate-timeout test",
        ]
        frozen: Any = plan
        with pytest.raises(dataclasses.FrozenInstanceError):
            frozen.script = None

    def test_detector_and_ceiling_launches_run_without_the_worker_scratch_vars(
        self, repo: Path, state: Path, home_stub: _Stub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = state / "scratch"
        scratch.mkdir()
        for name in gates._WORKER_SCRATCH_VARS:
            monkeypatch.setenv(name, str(scratch))
        envs: list[dict[str, str]] = []
        real_popen = subprocess.Popen

        def _spy(argv: list[str], *args: Any, **kwargs: Any) -> Any:
            envs.append(kwargs["env"])
            return real_popen(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", _spy)
        home_stub.gates(("lint", "true", 30))

        plan = plan_cycle_gates(repo, _SCOPE)

        assert plan.why_unavailable is None
        assert len(envs) >= 2
        for env in envs:
            assert not [n for n in gates._WORKER_SCRATCH_VARS if n in env]

    def test_zero_gates_is_a_plan_not_unavailable(
        self, repo: Path, home_stub: _Stub
    ) -> None:
        plan = plan_cycle_gates(repo, _SCOPE)

        assert plan.gates == ()
        assert plan.why_unavailable is None

    @pytest.mark.parametrize(
        ("overrides", "needle"),
        [
            ({"detect_exit": 3}, "exit"),
            ({"detect_raw": "not json"}, "JSON"),
            ({"detect_raw": json.dumps({"gates": [{"name": "a"}]})}, "malformed"),
            (
                {"detect_raw": json.dumps({"gates": [{"name": "", "command": "x"}]})},
                "malformed",
            ),
            ({"detect_raw": json.dumps({"gates": "nope"})}, "malformed"),
        ],
    )
    def test_detector_failures_are_unavailable(
        self,
        repo: Path,
        home_stub: _Stub,
        caplog: pytest.LogCaptureFixture,
        overrides: dict[str, object],
        needle: str,
    ) -> None:
        home_stub.set(**overrides)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            plan = plan_cycle_gates(repo, _SCOPE)

        assert plan.why_unavailable is not None
        assert needle in plan.why_unavailable
        warned = [r.getMessage() for r in caplog.records]
        assert any(
            "codex_fix_gates_unavailable" in m and "ticket=T-1" in m and "cycle=1" in m
            for m in warned
        )

    @pytest.mark.parametrize(
        "raw",
        [
            json.dumps({}),
            json.dumps({"foreground_ceiling_s": "5"}),
            json.dumps({"foreground_ceiling_s": True}),
            json.dumps({"foreground_ceiling_s": 0}),
            json.dumps({"foreground_ceiling_s": -4}),
            "garbage",
        ],
    )
    def test_bad_ceiling_is_unavailable(
        self, repo: Path, home_stub: _Stub, raw: str
    ) -> None:
        home_stub.gates(("lint", "true", 1))
        home_stub.set(ceiling_raw=raw)

        assert plan_cycle_gates(repo, _SCOPE).why_unavailable is not None

    def test_ceiling_lookup_failure_is_unavailable(
        self, repo: Path, home_stub: _Stub
    ) -> None:
        home_stub.gates(("lint", "true", 1))
        home_stub.set(ceiling_exit=1)

        assert plan_cycle_gates(repo, _SCOPE).why_unavailable is not None

    def test_popen_oserror_is_unavailable(
        self, repo: Path, home_stub: _Stub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object, **_k: object) -> None:
            msg = "no exec"
            raise OSError(msg)

        monkeypatch.setattr(subprocess, "Popen", _boom)

        plan = plan_cycle_gates(repo, _SCOPE)

        assert plan.why_unavailable is not None
        assert "no exec" in plan.why_unavailable


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class TestGateEnv:
    def test_scrubs_the_scratch_trio_and_git_vars_and_keeps_the_rest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in gates._WORKER_SCRATCH_VARS:
            monkeypatch.setenv(name, "/wt/.cw/tmp")
        monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
        monkeypatch.setenv("CW_GATE_ENV_MARKER", "kept")

        env = gates._gate_env()

        assert set(gates._WORKER_SCRATCH_VARS) == {"TMPDIR", "TMP", "TEMP"}
        assert not [n for n in gates._WORKER_SCRATCH_VARS if n in env]
        assert not [k for k in env if k.startswith("GIT_")]
        assert env["CW_GATE_ENV_MARKER"] == "kept"
        assert "PATH" in env
        assert env == {
            k: v
            for k, v in os.environ.items()
            if k not in gates._WORKER_SCRATCH_VARS and not k.startswith("GIT_")
        }

    @pytest.mark.parametrize("name", gates._WORKER_SCRATCH_VARS)
    def test_each_scratch_var_is_scrubbed_independently(
        self, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        for other in gates._WORKER_SCRATCH_VARS:
            monkeypatch.delenv(other, raising=False)
        monkeypatch.setenv(name, "/wt/.cw/tmp")

        assert name not in gates._gate_env()

    def test_leaves_the_parent_environment_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in gates._WORKER_SCRATCH_VARS:
            monkeypatch.setenv(name, "/wt/.cw/tmp")

        gates._gate_env()

        for name in gates._WORKER_SCRATCH_VARS:
            assert os.environ[name] == "/wt/.cw/tmp"

    def test_without_parent_scratch_vars_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in gates._WORKER_SCRATCH_VARS:
            monkeypatch.delenv(name, raising=False)

        env = gates._gate_env()

        assert not [n for n in gates._WORKER_SCRATCH_VARS if n in env]


class TestRunner:
    def test_green_plan_runs_in_order_in_the_worktree(
        self, repo: Path, state: Path
    ) -> None:
        log = state / "ran.log"
        plan = _plan(
            ("a", _log_cmd(log, "a", "pwd >> " + shlex.quote(str(log))), _CEILING),
            ("b", _log_cmd(log, "b"), _CEILING),
        )

        assert _run(plan, repo) is None

        lines = log.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "a"
        assert os.path.realpath(lines[1]) == os.path.realpath(repo)
        assert lines[2] == "b"

    def test_red_gate_fails_fast_with_exit_code_and_tail(
        self, repo: Path, state: Path
    ) -> None:
        log = state / "ran.log"
        plan = _plan(
            ("lint", "echo boom; exit 7", _CEILING),
            ("test", _log_cmd(log, "test"), _CEILING),
        )

        failure = _run(plan, repo)

        assert failure is not None
        assert (failure.kind, failure.gate, failure.exit_code) == ("red", "lint", 7)
        assert "boom" in failure.output
        assert not log.exists()

    def test_unavailable_plan_starts_nothing(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_popen(*_a: object, **_k: object) -> None:
            raise AssertionError

        monkeypatch.setattr(subprocess, "Popen", _no_popen)

        failure = _run(GatePlan.unavailable("script STALE"), repo)

        assert failure is not None
        assert failure.kind == "unavailable"
        assert failure.why == "script STALE"

    def test_popen_oserror_is_unavailable(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_popen = subprocess.Popen

        def _boom(argv: list[str], *args: Any, **kwargs: Any) -> Any:
            if argv[0] == "sh":
                msg = "cannot exec sh"
                raise OSError(msg)
            return real_popen(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", _boom)

        failure = _run(_plan(("a", "true", _CEILING)), repo)

        assert failure is not None
        assert failure.kind == "unavailable"
        assert failure.why is not None
        assert "cannot exec sh" in failure.why

    def test_zero_gates_warns_and_tokens_once(
        self, repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        tokens: list[str] = []

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert _run(_plan(), repo, tokens) is None
            assert _run(_plan(), repo, tokens) is None

        assert tokens == [_NONE_FRICTION]
        assert any(_NONE_FRICTION in r.getMessage() for r in caplog.records)

    def test_modified_tracked_file_is_dirtied(self, repo: Path) -> None:
        failure = _run(_plan(("fmt", "echo y >> tracked.py", _CEILING)), repo)

        assert failure is not None
        assert (failure.kind, failure.gate) == ("dirtied", "fmt")

    def test_untracked_file_is_dirtied(self, repo: Path) -> None:
        failure = _run(_plan(("gen", "echo y > generated.txt", _CEILING)), repo)

        assert failure is not None
        assert failure.kind == "dirtied"

    def test_edit_to_a_file_the_cycle_already_changed_is_dirtied(
        self, repo: Path
    ) -> None:
        (repo / "cycle.py").write_text("a = 1\n", encoding="utf-8")

        failure = _run(_plan(("fmt", "echo b = 2 >> cycle.py", _CEILING)), repo)

        assert failure is not None
        assert failure.kind == "dirtied"

    def test_gitignored_artifact_stays_green(self, repo: Path) -> None:
        assert _run(_plan(("cov", "echo x > .coverage", _CEILING)), repo) is None

    def test_gate_runs_without_the_worker_scratch_vars(
        self, repo: Path, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scratch = state / "scratch"
        scratch.mkdir()
        for name in gates._WORKER_SCRATCH_VARS:
            monkeypatch.setenv(name, str(scratch))
        seen = state / "seen.txt"
        body = (
            f'printf \'%s,%s,%s\' "${{TMPDIR-UNSET}}" "${{TMP-UNSET}}" '
            f'"${{TEMP-UNSET}}" > {shlex.quote(str(seen))}'
        )

        assert _run(_plan(("env", body, _CEILING)), repo) is None
        assert seen.read_text(encoding="utf-8") == "UNSET,UNSET,UNSET"

    def test_a_dirty_cycle_that_a_gate_leaves_alone_stays_green(
        self, repo: Path
    ) -> None:
        (repo / "cycle.py").write_text("a = 1\n", encoding="utf-8")

        assert _run(_plan(("noop", "true", _CEILING)), repo) is None

    def test_output_secrets_are_withheld(self, repo: Path) -> None:
        failure = _run(
            _plan(("t", "echo API_TOKEN=abc123xyz; echo visible; exit 1", _CEILING)),
            repo,
        )

        assert failure is not None
        assert "abc123xyz" not in failure.output
        assert "visible" in failure.output


class TestBreachText:
    def test_red(self) -> None:
        failure = GateFailure("red", "lint", "ruff", 4, 1.0, "tail text", None)

        breach = gate_failure_breach(failure, 2)

        assert breach.reason == CODEX_FIX_GATE_FAILED
        assert breach.restorable is True
        assert "lint" in breach.details
        assert "exit code 4" in breach.details
        assert "tail text" in breach.details
        assert "Nothing was committed" in breach.details
        assert LEFT_STAGED_HINT in breach.recovery_hint

    def test_dirtied(self) -> None:
        failure = GateFailure("dirtied", "fmt", "ruff format", 0, 1.0, "", None)

        breach = gate_failure_breach(failure, 1)

        assert "gate dirtied the worktree" in breach.details
        assert "fmt" in breach.details
        assert LEFT_STAGED_HINT in breach.recovery_hint

    def test_unavailable(self) -> None:
        failure = GateFailure("unavailable", "", "", None, 0.0, "", "script STALE")

        breach = gate_failure_breach(failure, 1)

        assert "could not be run" in breach.details
        assert "script STALE" in breach.details
        assert breach.reason == CODEX_FIX_GATE_FAILED


# ---------------------------------------------------------------------------
# Slow-gate signal (R18, R21): signal only, never killed
# ---------------------------------------------------------------------------


def _forbid_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no(*_a: object, **_k: object) -> None:
        msg = "a kill API was called"
        raise AssertionError(msg)

    for name in ("kill", "terminate", "send_signal"):
        monkeypatch.setattr(subprocess.Popen, name, _no)
    monkeypatch.setattr(os, "kill", _no)
    monkeypatch.setattr(os, "killpg", _no)


class TestSlowGate:
    def test_slow_green_gate_signals_once_and_finishes(
        self,
        repo: Path,
        state: Path,
        slow_events: list[tuple],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _forbid_kill(monkeypatch)
        marker = state / "done"
        cmd = f"sleep {_SLOW_SLEEP}; echo done > {shlex.quote(str(marker))}"

        assert _run(_plan(("slow", cmd, _SLOW_CEILING)), repo) is None

        assert marker.exists()
        assert len(slow_events) == 1
        _etype, payload, _corr = slow_events[0]
        assert payload["ticket_id"] == "T-1"
        assert payload["client"] == "test"
        assert payload["session_id"] == "s-1"
        assert payload["cycle"] == 1
        assert payload["gate"] == "slow"
        assert payload["elapsed_s"] >= _SLOW_CEILING
        assert payload["ceiling_s"] == _SLOW_CEILING

    def test_slow_red_gate_keeps_its_real_exit_code(
        self,
        repo: Path,
        slow_events: list[tuple],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _forbid_kill(monkeypatch)

        failure = _run(
            _plan(("slow", f"sleep {_SLOW_SLEEP}; exit 3", _SLOW_CEILING)), repo
        )

        assert len(slow_events) == 1
        assert failure is not None
        assert (failure.kind, failure.exit_code) == ("red", 3)

    def test_fast_gate_emits_nothing(
        self, repo: Path, slow_events: list[tuple]
    ) -> None:
        assert _run(_plan(("fast", "true", _CEILING)), repo) is None
        assert slow_events == []

    def test_one_event_per_slow_gate(
        self,
        repo: Path,
        slow_events: list[tuple],
    ) -> None:
        cmd = f"sleep {_SLOW_SLEEP}"

        assert (
            _run(_plan(("a", cmd, _SLOW_CEILING), ("b", cmd, _SLOW_CEILING)), repo)
            is None
        )

        assert [e[1]["gate"] for e in slow_events] == ["a", "b"]

    def test_slow_detector_and_ceiling_lookup_signal(
        self,
        repo: Path,
        home_stub: _Stub,
        slow_events: list[tuple],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gates, "SLOW_HELPER_SECONDS", 0.1)
        home_stub.gates(("lint", "true", 5))
        home_stub.set(detect_delay=0.4)

        plan = plan_cycle_gates(repo, _SCOPE)

        assert plan.gates == (PlannedGate("lint", "true", 5),)
        assert [e[1]["gate"] for e in slow_events] == ["detect-gates"]

        slow_events.clear()
        home_stub.set(detect_delay=0, ceiling_delay=0.4)

        plan = plan_cycle_gates(repo, _SCOPE)

        assert plan.gates == (PlannedGate("lint", "true", 5),)
        assert [e[1]["gate"] for e in slow_events] == ["gate-timeout lint"]

    def test_a_failed_emit_never_changes_the_outcome(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*_a: object, **_k: object) -> None:
            msg = "inbox unwritable"
            raise OSError(msg)

        monkeypatch.setattr(gates, "record_event", _fail)

        assert (
            _run(_plan(("slow", f"sleep {_SLOW_SLEEP}", _SLOW_CEILING)), repo) is None
        )

    def test_source_never_names_a_kill_mechanism(self) -> None:
        source = Path(gates.__file__).read_text(encoding="utf-8")
        for banned in (
            "killpg",
            "SIGKILL",
            "SIGTERM",
            "start_new_session",
            ".kill(",
            ".terminate(",
        ):
            assert banned not in source

    def test_teardown_sends_the_child_no_signal(
        self,
        repo: Path,
        state: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _forbid_kill(monkeypatch)
        marker = state / "survivor"
        cmd = f"sleep 0.6; echo alive > {shlex.quote(str(marker))}"
        real = gates._stream_new
        ticks = {"n": 0}

        def _interrupt(*args: object, **kwargs: object) -> object:
            ticks["n"] += 1
            if ticks["n"] == _INTERRUPT_TICK:
                raise KeyboardInterrupt
            return real(*args, **kwargs)

        monkeypatch.setattr(gates, "_stream_new", _interrupt)

        with pytest.raises(KeyboardInterrupt):
            _run(_plan(("sleeper", cmd, _CEILING)), repo)

        deadline = time.monotonic() + 1.5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()


class TestOutputStreaming:
    def test_output_is_logged_while_the_gate_runs_at_warning(
        self, repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cmd = "echo early; sleep 0.5; echo late"

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            assert _run(_plan(("chatty", cmd, _CEILING)), repo) is None

        out = [r for r in caplog.records if "codex_gate_output" in r.getMessage()]
        early = next(r for r in out if r.getMessage().endswith("| early"))
        late = next(r for r in out if r.getMessage().endswith("| late"))
        assert late.created - early.created >= _MIN_OUTPUT_GAP_S
        assert early.levelno >= logging.WARNING
        assert late.levelno >= logging.WARNING
        summary = [r for r in caplog.records if "codex_fix_gate " in r.getMessage()]
        assert summary
        assert all(r.levelno >= logging.WARNING for r in summary)
        assert "gate=chatty" in summary[0].getMessage()
        assert "exit=0" in summary[0].getMessage()

    def test_a_warning_root_handler_emits_the_output_line(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The detached `cw codex run` job logs at WARNING (no -v), so an INFO
        # record would never reach .cw/codex_driver.log.
        handler = logging.StreamHandler()
        handler.setLevel(logging.WARNING)
        logger = logging.getLogger(_LOGGER)
        logger.addHandler(handler)
        try:
            _run(_plan(("chatty", "echo visible-line", _CEILING)), repo)
        finally:
            logger.removeHandler(handler)

        assert "codex_gate_output" in capsys.readouterr().err

    def test_trailing_partial_line_is_flushed(
        self, repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _run(_plan(("p", "printf tail-no-newline", _CEILING)), repo)

        assert any(r.getMessage().endswith("| tail-no-newline") for r in caplog.records)


# ---------------------------------------------------------------------------
# Wiring through the real loop
# ---------------------------------------------------------------------------


@pytest.fixture
def real_gates(_codex_fix_gates_off: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commit, "run_cycle_gates", gates.run_cycle_gates)
    monkeypatch.setattr(commit, "plan_cycle_gates", gates.plan_cycle_gates)


@pytest.fixture
def loop_repo(repo: Path) -> Path:
    add_bare_origin(repo)
    return repo


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    *,
    runner: object | None = None,
    cycles: int = 1,
    **kwargs: object,
) -> tuple[AutoDevResult, object]:
    finding = _accepted(summary="bug", file="src/cw/producer.py")
    result, _verdict, fake = _drive_loop(
        monkeypatch,
        repo,
        [finding],
        [[finding]] * (cycles - 1) + [[]],
        runner=runner or _FakeRunner(edit=True),
        **kwargs,
    )
    return result, fake


def _origin_tip(repo: Path) -> str:
    git_in(repo, "fetch", "origin")
    return git_in(repo, "rev-parse", "origin/main")


class TestWiring:
    def test_green_gates_run_before_the_commit_and_the_cycle_pushes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loop_repo: Path,
        home_stub: _Stub,
        state: Path,
        real_gates: None,
    ) -> None:
        log = state / "ran.log"
        start = git_in(loop_repo, "rev-parse", "HEAD")
        head_seen = state / "head.txt"
        home_stub.gates(
            (
                "g",
                _log_cmd(
                    log, "g", f"git rev-parse HEAD > {shlex.quote(str(head_seen))}"
                ),
                5,
            )
        )

        result, _fake = _drive(monkeypatch, loop_repo)

        assert head_seen.read_text(encoding="utf-8").strip() == start
        assert git_in(loop_repo, "rev-parse", "HEAD") != start
        assert result.blocker is None or result.blocker.reason != CODEX_FIX_GATE_FAILED
        assert log.read_text(encoding="utf-8").splitlines() == ["g"]

    def test_red_gate_parks_pins_the_edits_and_does_not_rereview(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loop_repo: Path,
        home_stub: _Stub,
        real_gates: None,
    ) -> None:
        start = git_in(loop_repo, "rev-parse", "HEAD")
        origin = _origin_tip(loop_repo)
        home_stub.gates(("lint", "echo lint-went-wrong; exit 9", 5))

        result, fake = _drive(monkeypatch, loop_repo)

        assert result.blocker is not None
        assert result.blocker.reason == CODEX_FIX_GATE_FAILED
        assert "lint" in result.blocker.details
        assert "exit code 9" in result.blocker.details
        assert "lint-went-wrong" in result.blocker.details
        assert "refs/cw/refused-cycles/" in result.blocker.recovery_hint
        assert_clean_after_refusal(loop_repo, start)
        assert _origin_tip(loop_repo) == origin
        assert fake.calls == 0
        assert result.blocker.retry_eligible is not True

    def test_dirtied_gate_parks_with_a_clean_tree(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loop_repo: Path,
        home_stub: _Stub,
        real_gates: None,
    ) -> None:
        start = git_in(loop_repo, "rev-parse", "HEAD")
        home_stub.gates(("fmt", "echo x >> tracked.py", 5))

        result, _fake = _drive(monkeypatch, loop_repo)

        assert result.blocker is not None
        assert result.blocker.reason == CODEX_FIX_GATE_FAILED
        assert "dirtied" in result.blocker.details
        assert "refs/cw/refused-cycles/" in result.blocker.recovery_hint
        assert_clean_after_refusal(loop_repo, start)

    def test_no_op_cycle_never_runs_gates_even_when_unavailable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loop_repo: Path,
        real_gates: None,
    ) -> None:
        def _fail(*_a: object, **_k: object) -> None:
            msg = "gates ran on a no-op cycle"
            raise AssertionError(msg)

        monkeypatch.setattr(commit, "run_cycle_gates", _fail)

        result, _fake = _drive(monkeypatch, loop_repo, runner=_FakeRunner(edit=False))

        assert result.blocker is None or result.blocker.reason != CODEX_FIX_GATE_FAILED

    def test_unavailable_script_parks_a_committing_cycle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loop_repo: Path,
        real_gates: None,
    ) -> None:
        start = git_in(loop_repo, "rev-parse", "HEAD")

        result, _fake = _drive(monkeypatch, loop_repo)

        assert result.blocker is not None
        assert result.blocker.reason == CODEX_FIX_GATE_FAILED
        assert (
            "not found" in result.blocker.details or "STALE" in result.blocker.details
        )
        assert_clean_after_refusal(loop_repo, start)

    def test_git_error_in_the_gate_step_parks_as_a_codex_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loop_repo: Path,
        real_gates: None,
    ) -> None:
        def _git_boom(*_a: object, **_k: object) -> None:
            raise subprocess.CalledProcessError(2, ["true"], "", "git broke")

        monkeypatch.setattr(commit, "run_cycle_gates", _git_boom)

        result, _fake = _drive(monkeypatch, loop_repo)

        assert result.blocker is not None
        assert result.blocker.reason != CODEX_FIX_GATE_FAILED

    def test_zero_gates_over_two_cycles_leave_one_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loop_repo: Path,
        home_stub: _Stub,
        real_gates: None,
    ) -> None:
        tokens: list[str] = []

        _drive(monkeypatch, loop_repo, cycles=2, gate_tokens=tokens)

        assert tokens == [_NONE_FRICTION]


class _PinRunner:
    """A fix runner that rewrites the detector and CLAUDE.md during the fix turn."""

    def __init__(self, order: Path, sentinel: Path, script: Path) -> None:
        self._order = order
        self._sentinel = sentinel
        self._script = script
        self.fix_calls = 0

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        if "workspace-write" in argv:
            self.fix_calls += 1
            with self._order.open("a", encoding="utf-8") as handle:
                handle.write("fix\n")
            self._script.write_text(
                "import pathlib, sys\n"
                f"pathlib.Path({str(self._sentinel)!r}).write_text('ran')\n"
                "print('gate-timeout')\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            (worktree / "CLAUDE.md").write_text(
                "# rewritten\n- evil\n", encoding="utf-8"
            )
            (worktree / "fix_cycle.py").write_text("x = 1\n", encoding="utf-8")
        return CodexRunResult(returncode=0, stdout="", stderr="")


class TestCycleStartPin:
    def test_a_fix_cannot_change_which_gates_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loop_repo: Path,
        state: Path,
        real_gates: None,
    ) -> None:
        sentinel = state / "sentinel"
        stub = _Stub(loop_repo / ".claude" / "scripts", state)
        (loop_repo / "CLAUDE.md").write_text("# gates\n- pytest\n", encoding="utf-8")
        git_in(loop_repo, "add", "-A")
        git_in(loop_repo, "commit", "-m", "add detector and CLAUDE.md")
        git_in(loop_repo, "push", "origin", "main")
        # The detector's call log doubles as the ordered event log: the fix
        # runner and each gate append to it too.
        shared = stub.calls_path
        stub.gates(
            ("a", _log_cmd(shared, "gate:a"), 5),
            ("b", _log_cmd(shared, "gate:b"), 5),
        )
        runner = _PinRunner(shared, sentinel, stub.script)

        result, _fake = _drive(monkeypatch, loop_repo, runner=runner)

        assert result.blocker is None or result.blocker.reason != CODEX_FIX_GATE_FAILED
        assert not sentinel.exists()
        events = shared.read_text(encoding="utf-8").splitlines()
        assert events == [
            "detect-gates",
            "gate-timeout a",
            "gate-timeout b",
            "fix",
            "gate:a",
            "gate:b",
        ]
