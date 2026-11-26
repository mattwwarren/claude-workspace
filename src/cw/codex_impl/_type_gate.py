"""Codex IMPL gate 4: run ruff and mypy on the touched Python files (#3531).

:func:`run_type_check_gate` runs after the repo's quality gates and before
cw's one commit, in a detached temporary worktree at a dangling snapshot
commit of the staged tree. Applicability comes from the gate commands pinned
before the turn: a tool is checked only when a declared gate runs it through
``uv run``. Every command is a single-gate plan handed to
:func:`cw.codex_fix_loop.gates.run_cycle_gates`, so it gets the scrubbed env,
a private ``TMPDIR``, redaction and the signal-only ceiling (ADR-0014: no
timeout). Each command's whole output goes to a file through a shell redirect,
because the gate runner keeps only a capped tail. The pieces that are not
codex-specific live in :mod:`cw.stage_verification.impl_type_gate`.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cw.codex_fix_loop.gates import GatePlan, PlannedGate, run_cycle_gates
from cw.review_adjudication import DEFERRED_FINDINGS_RELATIVE_PATH
from cw.stage_verification.baseline import staged_cycle_tree
from cw.stage_verification.impl_type_gate import (
    BASELINE_FRICTION_PREFIX,
    BASELINE_TOOL,
    CONSUMER_UNKNOWN_PREFIX,
    LAUNCHER_UNSUPPORTED_PREFIX,
    MYPY,
    MYPY_NOT_DECLARED_FRICTION,
    RECORD_FAILED_FRICTION,
    RUFF,
    RUFF_NOT_DECLARED_FRICTION,
    SKIPPED_NO_GATE_FRICTION,
    UNTOUCHED_ONLY_PREFIX,
    MypyBaseline,
    VerdictKind,
    baseline_command_block,
    baseline_growth,
    consumer_launch,
    declares_tool,
    detect_mypy_baseline,
    gate_fork_point,
    gate_output_dir,
    gate_root,
    gate_worktree,
    growth_block,
    is_baseline_consumer,
    is_python,
    launch_over,
    listed_files,
    mypy_failed_block,
    mypy_touched_block,
    mypy_verdict,
    parse_mypy_errors,
    path_args,
    postable_command,
    prepare_failed_block,
    prepare_unavailable_block,
    prepare_unreadable_block,
    record_baselined_errors,
    ruff_block,
    setup_block,
    snapshot_commit,
    touched_python_files,
    unavailable_block,
    why_of,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from pathlib import Path

    from cw.codex_impl._run import _Flow, _Run
    from cw.stage_verification.impl_block import ImplBlock
    from cw.stage_verification.impl_type_gate import Launch

_log = logging.getLogger(__name__)

_PREPARE: Final = "impl-gate4-prepare"
_RUFF: Final = "impl-gate4-ruff"
_MYPY: Final = "impl-gate4-mypy"
_BASELINE: Final = "impl-gate4-mypy-baseline"
_RECORD: Final = "impl-gate4-mypy-record"
_MYPY_FLAGS: Final = ("--no-pretty", "--no-color-output", "--hide-absolute-path")
_DEFAULT_CEILING_S: Final = 600.0


@dataclass(frozen=True)
class _Declared:
    """How the pinned gates launch ruff, mypy and a baseline consumer."""

    ruff: Launch | None
    mypy: Launch | None
    consumer: Launch | None

    @property
    def active(self) -> bool:
        return any(
            launch is not None for launch in (self.ruff, self.mypy, self.consumer)
        )


@dataclass(frozen=True)
class _State:
    """The snapshot, its fork point, the baseline state and the touched files."""

    snap: str
    fork: str
    baseline: MypyBaseline | None
    files: tuple[str, ...]


@dataclass(frozen=True)
class _Steps:
    """Which checks run: ruff, plain mypy, and the baseline commands (in order)."""

    ruff: bool = False
    plain_mypy: bool = False
    baseline: tuple[str, ...] = ()

    @property
    def any(self) -> bool:
        return self.ruff or self.plain_mypy or bool(self.baseline)


@dataclass(frozen=True)
class _Ctx:
    """One gate 4 run: the run, its flow, the measured state and the dirs."""

    run: _Run
    flow: _Flow
    state: _State
    tmp: Path
    out: Path


@dataclass(frozen=True)
class _Ran:
    """A finished command: exit code and full output, or why it did not run."""

    exit_code: int
    output: str
    why: str | None = None


def _declared(commands: Sequence[str]) -> _Declared:
    return _Declared(
        ruff=launch_over(commands, RUFF),
        mypy=launch_over(commands, MYPY),
        consumer=consumer_launch(commands),
    )


def _measure(run: _Run, flow: _Flow, commands: Sequence[str]) -> _State:
    """Snapshot the staged tree and derive the fork, baseline and files (git)."""
    tree = staged_cycle_tree(run.worktree, flow.baseline.review_artifacts)
    snap = snapshot_commit(run.worktree, tree)
    fork = gate_fork_point(run.worktree, run.base_branch, snap)
    return _State(
        snap=snap,
        fork=fork,
        baseline=detect_mypy_baseline(run.worktree, fork, commands),
        files=touched_python_files(run.worktree, fork, snap),
    )


def _growth(run: _Run, state: _State) -> ImplBlock | None:
    """The no-growth check on every tracked baseline file (git only)."""
    if state.baseline is None:
        return None
    for path in state.baseline.files:
        added = baseline_growth(run.worktree, state.fork, state.snap, path)
        if added:
            return growth_block(path, added)
    return None


def _tool_on(
    launch: Launch | None, tool: str, tokens: list[str], not_declared: str
) -> bool:
    """Whether *tool*'s check runs; adds the skip friction when it does not."""
    if launch == "uv":
        return True
    tokens.append(
        not_declared if launch is None else f"{LAUNCHER_UNSUPPORTED_PREFIX}{tool}"
    )
    return False


def _baseline_steps(
    baseline: MypyBaseline, declared: _Declared, tokens: list[str]
) -> tuple[str, ...]:
    """The detected or consumer-unknown baseline's mypy commands (maybe none)."""
    files = listed_files(baseline.files)
    if not baseline.commands:
        tokens.append(f"{CONSUMER_UNKNOWN_PREFIX}{files}")
        return ()
    shown = "; ".join(postable_command(command) for command in baseline.commands)
    tokens.append(f"{BASELINE_FRICTION_PREFIX}file={files} cmd={shown}")
    if _tool_on(declared.consumer, BASELINE_TOOL, tokens, MYPY_NOT_DECLARED_FRICTION):
        return baseline.commands
    return ()


def _steps(declared: _Declared, state: _State, tokens: list[str]) -> _Steps:
    """The checks to run; no touched Python file runs none."""
    if not state.files:
        return _Steps()
    ruff = _tool_on(declared.ruff, RUFF, tokens, RUFF_NOT_DECLARED_FRICTION)
    if state.baseline is not None:
        return _Steps(
            ruff=ruff, baseline=_baseline_steps(state.baseline, declared, tokens)
        )
    plain = _tool_on(declared.mypy, MYPY, tokens, MYPY_NOT_DECLARED_FRICTION)
    return _Steps(ruff=ruff, plain_mypy=plain)


def _ceiling(flow: _Flow, *tools: str) -> float:
    """The signal ceiling of the first pinned gate declaring one of *tools*."""
    for gate in flow.gate_plan.gates:
        if any(declares_tool(gate.command, tool) for tool in tools):
            return gate.ceiling_s
        if BASELINE_TOOL in tools and is_baseline_consumer(gate.command):
            return gate.ceiling_s
    return _DEFAULT_CEILING_S


def _wrapped(command: str, out_file: Path) -> str:
    """*command* with its whole output saved to *out_file*, echoed, exit kept."""
    quoted = shlex.quote(str(out_file))
    return f"(\n{command}\n) > {quoted} 2>&1\nrc=$?\ncat {quoted}\nexit $rc\n"


def _read(out_file: Path) -> str:
    try:
        return out_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _run_one(ctx: _Ctx, label: str, command: str, ceiling_s: float) -> _Ran:
    """Run *command* in the gate worktree through the shared gate runner."""
    out_file = ctx.out / f"{label}.out"
    gate = PlannedGate(
        name=label,
        command=_wrapped(command, out_file),
        ceiling_s=ceiling_s,
        accept_hook_rewrite=False,
    )
    failure = run_cycle_gates(
        GatePlan(script=None, gates=(gate,), why_unavailable=None),
        worktree=ctx.tmp,
        review_artifacts=frozenset(),
        scope=ctx.flow.scope,
        tokens=[],
        touched=frozenset(),
    )
    if failure is not None and failure.kind == "unavailable":
        return _Ran(exit_code=-1, output="", why=failure.why or "no reason recorded")
    # A dirtied pass is a pass: the gate worktree is disposable.
    exit_code = failure.exit_code if failure is not None else 0
    return _Ran(exit_code=exit_code or 0, output=_read(out_file))


def _mypy_command(files: Sequence[str]) -> str:
    return shlex.join(["uv", "run", "mypy", *_MYPY_FLAGS, *path_args(files)])


def _run_prepare(ctx: _Ctx) -> ImplBlock | None:
    pin = ctx.flow.prepare
    if pin.error is not None:
        return prepare_unreadable_block(pin.error)
    if pin.command is None:
        return None
    command = shlex.join(["bash", "-c", pin.command])
    ran = _run_one(
        ctx, _PREPARE, command, _ceiling(ctx.flow, RUFF, MYPY, BASELINE_TOOL)
    )
    if ran.why is not None:
        return prepare_unavailable_block(ran.why)
    if ran.exit_code != 0:
        return prepare_failed_block(pin.command, ran.exit_code, ran.output)
    return None


def _run_ruff(ctx: _Ctx) -> ImplBlock | None:
    files = ctx.state.files
    command = shlex.join(
        ["uv", "run", "ruff", "check", "--force-exclude", *path_args(files)]
    )
    ran = _run_one(ctx, _RUFF, command, _ceiling(ctx.flow, RUFF))
    if ran.why is not None:
        return unavailable_block(command, ran.why)
    if ran.exit_code != 0:
        return ruff_block(command, ran.exit_code, ran.output, files)
    return None


def _untouched_only(ctx: _Ctx, files: Sequence[str]) -> None:
    shown = listed_files(files)
    ctx.flow.tokens.append(f"{UNTOUCHED_ONLY_PREFIX}{shown}")
    _log.warning(
        "impl_gate4_mypy_untouched_only ticket=%s files=%s",
        ctx.flow.scope.ticket_id,
        shown,
    )


def _run_plain_mypy(ctx: _Ctx) -> ImplBlock | None:
    files = ctx.state.files
    command = _mypy_command(files)
    ran = _run_one(ctx, _MYPY, command, _ceiling(ctx.flow, MYPY))
    if ran.why is not None:
        return unavailable_block(command, ran.why)
    verdict = mypy_verdict(ran.exit_code, parse_mypy_errors(ran.output, ctx.tmp), files)
    if verdict.kind is VerdictKind.TOUCHED:
        return mypy_touched_block(command, ran.exit_code, verdict.touched_errors, files)
    if verdict.kind is VerdictKind.UNPARSED:
        return mypy_failed_block(command, ran.exit_code, ran.output, files)
    if verdict.kind is VerdictKind.UNTOUCHED_ONLY:
        _untouched_only(ctx, verdict.untouched_files)
    return None


def _record(ctx: _Ctx) -> None:
    """The baselined-debt record: never blocks, friction on any failure."""
    files = ctx.state.files
    ran = _run_one(ctx, _RECORD, _mypy_command(files), _ceiling(ctx.flow, MYPY))
    errors = parse_mypy_errors(ran.output, ctx.tmp) if ran.why is None else []
    if ran.why is not None or (ran.exit_code != 0 and not errors):
        ctx.flow.tokens.append(RECORD_FAILED_FRICTION)
        return
    path = ctx.run.worktree / DEFERRED_FINDINGS_RELATIVE_PATH
    ctx.flow.tokens.extend(record_baselined_errors(path, errors, files))


def _run_baseline(ctx: _Ctx, commands: Sequence[str]) -> ImplBlock | None:
    ceiling = _ceiling(ctx.flow, BASELINE_TOOL)
    for index, command in enumerate(commands):
        ran = _run_one(ctx, f"{_BASELINE}-{index}", command, ceiling)
        if ran.why is not None:
            return unavailable_block(command, ran.why)
        if ran.exit_code != 0:
            return baseline_command_block(
                command, ran.exit_code, ran.output, ctx.state.fork, ctx.state.files
            )
    _record(ctx)
    return None


def _execute(ctx: _Ctx, steps: _Steps) -> ImplBlock | None:
    """``tests.prepare``, then ruff, then mypy (plain or baseline); fail fast."""
    block = _run_prepare(ctx)
    if block is None and steps.ruff:
        block = _run_ruff(ctx)
    if block is None and steps.plain_mypy:
        block = _run_plain_mypy(ctx)
    if block is None and steps.baseline:
        block = _run_baseline(ctx, steps.baseline)
    return block


def _in_gate_worktree(
    run: _Run, flow: _Flow, state: _State, steps: _Steps
) -> ImplBlock | None:
    root = gate_root()
    ticket = flow.scope.ticket_id
    try:
        with (
            gate_output_dir(root, ticket) as out,
            gate_worktree(run.worktree, state.snap, root=root, ticket=ticket) as tmp,
        ):
            return _execute(_Ctx(run, flow, state, tmp, out), steps)
    except (subprocess.CalledProcessError, OSError) as exc:
        return setup_block(why_of(exc))


def _log_state(flow: _Flow, declared: _Declared, state: _State) -> None:
    baseline = state.baseline
    _log.warning(
        "impl_gate4 ticket=%s baseline=%s consumer=%s py_files=%d",
        flow.scope.ticket_id,
        listed_files(baseline.files) if baseline is not None else "none",
        "yes" if declared.consumer is not None else "no",
        len(state.files),
    )


def run_type_check_gate(
    run: _Run, flow: _Flow, touched: Collection[str]
) -> ImplBlock | None:
    """Step 2.5 gate 4: ruff and mypy on the touched Python files.

    *touched* is the turn's measured path set; it only decides the
    no-gate-declared friction. The checked files are every ``.py``/``.pyi``
    the branch changes since its fork point, earlier commits included.
    Returns the ``impl_failed`` block, or ``None`` (friction on *flow*).
    """
    commands = tuple(gate.command for gate in flow.gate_plan.gates)
    declared = _declared(commands)
    if not declared.active:
        if any(is_python(path) for path in touched):
            flow.tokens.append(SKIPPED_NO_GATE_FRICTION)
        return None
    try:
        state = _measure(run, flow, commands)
        _log_state(flow, declared, state)
        block = _growth(run, state)
    except (subprocess.CalledProcessError, OSError) as exc:
        return setup_block(why_of(exc))
    if block is not None:
        return block
    steps = _steps(declared, state, flow.tokens)
    if not steps.any:
        return None
    return _in_gate_worktree(run, flow, state, steps)


__all__ = ["run_type_check_gate"]
