"""The repo's quality gates, run on a fix cycle before it is committed (#3056).

A codex fix cycle used to commit whatever passed the scope fence and the growth
guards, so a fix that broke lint or a test reached the push and the re-review.
This module plans the repo's gates once per cycle, at cycle start, and runs
them on the cycle's changes before the commit. A failing gate parks
``codex_fix_gate_failed`` through the same preserve-and-reset path every other
restorable breach takes (:mod:`cw.codex_fix_loop.cycle_restore`).

**Detection is pinned at cycle start.** :func:`plan_cycle_gates` resolves
``prep_pr_state.py`` (the same probe order and stale check as ``/prep-pr``
Step 2), runs ``detect-gates`` once, and reads each gate's ``gate-timeout``
once, while the worktree is the clean cycle-start tree and before the fix turn.
The result is a frozen :class:`GatePlan`, the only input to
:func:`run_cycle_gates`, so a fix that edits ``CLAUDE.md`` or the detector
itself cannot change which gate commands run in its own cycle. The gate
commands still execute the fix's code (a test runs the code under test); that
is inherent, and is not sandboxed here. Each gate and autofix subprocess runs
with its own private scratch dir as ``TMPDIR`` (#3646), so concurrent gate runs
for different worktrees never share, and never prune, one pytest basetemp.

**A clock only emits a signal.** ``gate-timeout`` is a ceiling at which
:func:`_run_watched` emits one ``codex_fix.gate_slow`` event and one WARNING,
then keeps waiting for the real exit code. Elapsed time never stops a process,
parks the task, or resets the worktree (ADR-0014). A gate that truly hangs
holds its review until the operator stops the codex job; see
``docs/dispatch-runbook.md``. The only exits are evidence: a non-zero gate, a
gate that dirtied the tree, and a detector that is missing, stale, failing or
unreadable.

**A declared autofix is run on a red gate (#3412).** A gate that fails red and
declares an autofix (``detect-gates`` ``autofix``, else the exact-name
``quality_gates.autofix.<gate-name>`` of ``.claude/project-config.yaml``, pinned
at cycle start) gets that autofix run, then the SAME gate command re-checked.
Only a green re-check lets the cycle go on; anything else reverts the autofix's
changes and returns the ORIGINAL failure, so the park text and the pinned edits
are exactly as without an autofix. The autofix runs only when the failure is
``red`` and the failing check left the tree alone, it must exit 0, change at
least one path, change only paths already in the cycle's measured ``touched``
set (which passed the scope, fence and growth checks), and delete none (a
rename counts as a deletion, #3427). Format-only is therefore
a property proved by a declaration plus a green re-check, never guessed from a
gate's name or output. Feeding a failing gate's output to a later fix turn was
considered and rejected here: a park ends the loop, so no such turn exists
in-process, and a repair turn would cost a full ``codex exec`` and change how
non-format failures park. A config problem means no config-declared autofix,
never an unavailable plan. The autofix is signal-only like a gate (ADR-0014).

A child's output goes to an anonymous spool file, never a pipe: a file has no
backpressure and no broken pipe, so the child survives anything that happens to
cw's reader. Gate subprocesses run on the review thread holding no lock
(ADR-0019).
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from cw._git import git_clean_env
from cw.codex_fix_loop.baseline import staged_cycle_tree
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT, FenceBreach
from cw.codex_fix_loop.gate_autofix import (
    changed_paths,
    declared_autofixes,
    ignored_on_disk,
    revert_to_tree,
)
from cw.codex_fix_loop.posted_text import redact_and_cap, withhold_secret_lines
from cw.codex_review import CODEX_FIX_GATE_FAILED
from cw.events import record_event
from cw.exceptions import CwError
from cw.models.enums import OrchestratorEventType

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

_log = logging.getLogger(__name__)

# The ceiling at which a slow ``detect-gates`` or ``gate-timeout`` lookup
# signals. Signal only, like a gate's own ceiling.
SLOW_HELPER_SECONDS = 60.0
_POLL_SECONDS = 0.25
_READ_CHUNK_BYTES = 65536
_TAIL_LINES = 20
_TAIL_MAX_CHARS = 1500
_COMMAND_MAX_CHARS = 200
# The scratch-dir variables the worker convention force-sets to
# ``<worktree>/.cw/tmp`` (``cw.worktree.apply_worker_tmpdir``). ``tempfile``
# falls back from TMPDIR to TEMP then TMP, so all three go (#3392). They are
# scrubbed from the base env, then re-set to a per-run private dir by
# ``_gate_scratch_env`` (#3646).
_WORKER_SCRATCH_VARS = ("TMPDIR", "TMP", "TEMP")
# Where a gate's private scratch dir is minted: Python's own POSIX fallback
# order for the default tmp dir, hard-coded because the process env cannot be
# consulted (it carries the worker's ``<worktree>/.cw/tmp``). A constant so a
# test can point it at a throwaway root.
_SCRATCH_ROOT_CANDIDATES = tuple(f"{os.sep}{p}" for p in ("tmp", "var/tmp", "usr/tmp"))
_SCRATCH_PREFIX = "cw-gate-"

# `/prep-pr` Step 2's probe order: the repo copies first, the installed path
# last; the first file that exists wins, and a stale first hit stops the step.
REPO_SCRIPT_CANDIDATES = (
    ".claude/scripts/prep_pr_state.py",
    "scripts/prep_pr_state.py",
)
_HOME_SCRIPT = Path(".claude") / "scripts" / "prep_pr_state.py"
DETECT_GATES_COMMAND = "detect-gates"
GATE_TIMEOUT_COMMAND = "gate-timeout"
STALE_MARKER = GATE_TIMEOUT_COMMAND
_PROBED = ".claude/scripts/, scripts/, ~/.claude/scripts/"

NONE_DETECTED_FRICTION = "codex_fix_gates_none_detected"
GATE_AUTOFIXED_FRICTION = "codex_fix_gate_autofixed"
_LOG_POINTER = (
    "The full gate output was streamed to .cw/codex_driver.log in the cycle "
    "worktree (WARNING lines from logger cw.codex_fix_loop.gates)."
)

GateKind = Literal["red", "dirtied", "unavailable"]


class GateUnavailableError(Exception):
    """The gates could not be planned: a missing, stale or failing detector."""


@dataclass(frozen=True)
class GateScope:
    """The ids a gate log line and the slow-gate event are labelled with."""

    ticket_id: str
    client: str
    session_id: str
    cycle: int


@dataclass(frozen=True)
class PlannedGate:
    """One detected gate: name, shell command, signal ceiling (seconds), autofix.

    ``autofix`` is the declared command to run when the gate fails red (#3412),
    or ``None`` when the gate declares none.
    """

    name: str
    command: str
    ceiling_s: float
    autofix: str | None = None


@dataclass(frozen=True)
class GatePlan:
    """The cycle-start gate pin: immutable, and all :func:`run_cycle_gates` reads.

    ``why_unavailable`` is set when the script, the detector or a ceiling
    lookup failed; the cycle then parks if it reaches the commit step.
    """

    script: Path | None
    gates: tuple[PlannedGate, ...]
    why_unavailable: str | None

    @classmethod
    def unavailable(cls, why: str) -> GatePlan:
        """A plan that parks every committing cycle, naming *why*."""
        return cls(script=None, gates=(), why_unavailable=why)


@dataclass(frozen=True, kw_only=True)
class GateFailure:
    """Why a cycle's gates refused it; ``kind`` picks the park text.

    Keyword-only (#3377): seven fields, several of the same type, so a
    positional call that swaps two of them must be a ``TypeError``.
    """

    kind: GateKind
    gate: str
    command: str
    exit_code: int | None
    seconds: float
    output: str
    why: str | None


class _Watched(NamedTuple):
    """What :func:`_run_watched` observed of one finished subprocess."""

    returncode: int
    tail: str
    seconds: float
    text: str


@dataclass
class _Output:
    """The streaming state of one subprocess's spool file."""

    scope: GateScope
    gate: str
    log: bool
    offset: int = 0
    partial: bytes = b""
    tail: deque[str] = field(default_factory=lambda: deque(maxlen=_TAIL_LINES))
    chunks: list[bytes] | None = None


def resolve_prep_pr_state(worktree: Path) -> Path:
    """Return the first existing ``prep_pr_state.py`` of the probe order.

    Mirrors ``/prep-pr`` Step 2 (pinned by a drift test): ``.claude/scripts/``
    then ``scripts/`` in *worktree*, then ``~/.claude/scripts/``. A first hit
    without the ``gate-timeout`` subcommand is STALE and is not skipped.
    """
    candidates = [worktree / rel for rel in REPO_SCRIPT_CANDIDATES]
    candidates.append(Path.home() / _HOME_SCRIPT)
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            msg = f"{candidate} could not be read: {exc}"
            raise GateUnavailableError(msg) from exc
        if STALE_MARKER not in text:
            msg = (
                f"STALE: {candidate} predates #1432 (no {STALE_MARKER} "
                "subcommand); reinstall it or use the repo copy"
            )
            raise GateUnavailableError(msg)
        return candidate
    msg = f"prep_pr_state.py not found (probed {_PROBED})"
    raise GateUnavailableError(msg)


def _emit_slow(scope: GateScope, gate: str, elapsed: float, ceiling_s: float) -> None:
    """Signal that *gate* outlived its ceiling; mutates nothing else.

    A failed emit is logged and swallowed, so a signal can never change a gate's
    outcome (the same tuple as ``cycle_restore._record_preserved``).
    """
    _log.warning(
        "codex_fix_gate_slow ticket=%s session=%s cycle=%s gate=%s elapsed=%.1f "
        "ceiling=%.1f",
        scope.ticket_id,
        scope.session_id,
        scope.cycle,
        gate,
        elapsed,
        ceiling_s,
    )
    payload = {
        "ticket_id": scope.ticket_id,
        "client": scope.client,
        "session_id": scope.session_id,
        "cycle": scope.cycle,
        "gate": gate,
        "elapsed_s": round(elapsed, 2),
        "ceiling_s": ceiling_s,
    }
    try:
        record_event(OrchestratorEventType.CODEX_FIX_GATE_SLOW, payload)
    except (
        subprocess.CalledProcessError,
        CwError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        _log.warning("failed to record codex_fix.gate_slow event", exc_info=True)


def _take_line(out: _Output, raw: bytes) -> None:
    """Keep one complete output line for the tail and log it at WARNING.

    WARNING on purpose: the detached ``cw codex run`` job runs at the default
    WARNING root level (it is started without ``-v``), so an INFO record would
    never reach ``.cw/codex_driver.log``.
    """
    line = raw.decode("utf-8", errors="replace").rstrip("\r")
    out.tail.append(line)
    if out.log:
        safe_line = redact_and_cap(
            withhold_secret_lines(line), max_total_chars=_TAIL_MAX_CHARS
        )
        _log.warning(
            "codex_gate_output ticket=%s cycle=%s gate=%s | %s",
            out.scope.ticket_id,
            out.scope.cycle,
            out.gate,
            safe_line,
        )


def _stream_new(out: _Output, fd: int, *, final: bool = False) -> None:
    """Read what the child appended since the last call and log its lines.

    ``os.pread`` leaves the file offset the child shares alone. With *final*,
    a trailing partial line is flushed too.
    """
    while True:
        chunk = os.pread(fd, _READ_CHUNK_BYTES, out.offset)
        if not chunk:
            break
        out.offset += len(chunk)
        if out.chunks is not None:
            out.chunks.append(chunk)
        *lines, out.partial = (out.partial + chunk).split(b"\n")
        for raw in lines:
            _take_line(out, raw)
    if final and out.partial:
        _take_line(out, out.partial)
        out.partial = b""


def _wait_signalling(
    proc: subprocess.Popen[bytes],
    out: _Output,
    fd: int,
    *,
    start: float,
    ceiling_s: float,
) -> int:
    """Wait for *proc* with no upper bound, streaming its output as it grows.

    The first time the elapsed time reaches *ceiling_s* it emits one slow-gate
    signal and keeps waiting; nothing else changes.
    """
    signalled = False
    while True:
        try:
            return proc.wait(timeout=_POLL_SECONDS)
        except subprocess.TimeoutExpired:
            _stream_new(out, fd)
            elapsed = time.monotonic() - start
            if not signalled and elapsed >= ceiling_s:
                signalled = True
                _emit_slow(out.scope, out.gate, elapsed, ceiling_s)


def _gate_env() -> dict[str, str]:
    """The environment every gate-runner subprocess gets: the CI one.

    ``git_clean_env()`` (no ``GIT_*``) minus the worker scratch-dir
    variables ``TMPDIR``, ``TMP`` and ``TEMP``. A worker sets all three to
    ``<worktree>/.cw/tmp`` (``apply_worker_tmpdir``), which puts pytest's
    basetemp inside the worktree's own git work tree and under ``$HOME``;
    tests that assume a tmp dir outside both then fail on a clean tree and
    park every cycle (#3392). Removing ``TMPDIR`` alone is not enough:
    ``tempfile.gettempdir()`` falls back to ``TEMP`` and then ``TMP``. Unset,
    the platform default applies, as in CI. A gate that needs a
    project-local tmp dir must create it itself. Every other variable is
    kept. This is the scrub-only base: the ``detect-gates``/``gate-timeout``
    launches use it directly, and :func:`_gate_scratch_env` builds the gate and
    autofix env on top of it.
    """
    env = git_clean_env()
    for name in _WORKER_SCRATCH_VARS:
        env.pop(name, None)
    return env


def _scratch_root() -> str:
    """The first usable scratch parent, independent of the process env.

    Never ``tempfile.gettempdir()``: the detached ``cw codex run`` driver has
    ``TMPDIR``/``TMP``/``TEMP`` set to ``<worktree>/.cw/tmp``, so the platform
    default would land inside the worktree and undo #3392.
    """
    for candidate in _SCRATCH_ROOT_CANDIDATES:
        if Path(candidate).is_dir() and os.access(candidate, os.W_OK | os.X_OK):
            return candidate
    msg = f"no usable scratch root among {', '.join(_SCRATCH_ROOT_CANDIDATES)}"
    raise FileNotFoundError(msg)


@contextmanager
def _gate_scratch_env(label: str) -> Iterator[dict[str, str]]:
    """Yield the gate env with ``TMPDIR``/``TMP``/``TEMP`` on a private dir.

    Concurrent gate runs (one per worktree) used to share the platform default
    tmp root, so pytest's startup pruning of ``/tmp/pytest-of-<user>`` could
    delete a live sibling run's basetemp (#3646). Each subprocess now gets its
    own ``cw-gate-*`` dir (mode 0700) under :func:`_scratch_root`, outside the
    worktree and ``$HOME`` as #3392 requires, removed on every exit. *label* is
    the gate name, so a leftover warning says whose dir it was.

    Gates are never killed (ADR-0014): if cw's thread is torn down mid-wait the
    ``finally`` removes the dir under a still-running child whose result is
    discarded anyway. A hard-killed cw leaves a stale ``cw-gate-*`` dir, not swept.
    """
    scratch = tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=_scratch_root())
    try:
        env = _gate_env()
        for name in _WORKER_SCRATCH_VARS:
            env[name] = scratch
        yield env
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        if Path(scratch).exists():
            _log.warning(
                "codex_fix_gate_scratch_leftover gate=%s path=%s", label, scratch
            )


def _run_watched(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    ceiling_s: float,
    gate: str,
    scope: GateScope,
    helper: bool = False,
) -> _Watched:
    """Run *argv* to completion, signalling once if it outlives *ceiling_s*.

    The only place a clock is read. There is no upper bound on the wait and no
    signal to the child on any path: if cw's own thread is torn down by an
    exception while waiting, the exception propagates unchanged and the child
    runs to completion against its own descriptor. No ``with Popen``: its exit
    can wait on, or interrupt, the child. *helper* collects the full output
    for the caller to parse instead of logging it line by line.
    """
    start = time.monotonic()
    out = _Output(scope=scope, gate=gate, log=not helper, chunks=[] if helper else None)
    with tempfile.TemporaryFile("a+b") as spool:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=spool,
            stderr=subprocess.STDOUT,
        )
        fd = spool.fileno()
        returncode = _wait_signalling(proc, out, fd, start=start, ceiling_s=ceiling_s)
        _stream_new(out, fd, final=True)
    text = b"".join(out.chunks).decode("utf-8", errors="replace") if out.chunks else ""
    return _Watched(returncode, "\n".join(out.tail), time.monotonic() - start, text)


def _helper_output(
    script: Path, args: Sequence[str], *, worktree: Path, scope: GateScope
) -> str:
    """Run one ``prep_pr_state.py`` subcommand and return its output."""
    label = " ".join(args)
    watched = _run_watched(
        [sys.executable, str(script), *args],
        cwd=worktree,
        env=_gate_env(),
        ceiling_s=SLOW_HELPER_SECONDS,
        gate=label,
        scope=scope,
        helper=True,
    )
    if watched.returncode != 0:
        msg = (
            f"`prep_pr_state.py {label}` exited with exit code "
            f"{watched.returncode}: {_postable(watched.tail)}"
        )
        raise GateUnavailableError(msg)
    return watched.text


def _json_object(text: str, label: str) -> dict[str, Any]:
    """Decode *text* as a JSON object, or say why the helper is unusable."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        msg = f"`prep_pr_state.py {label}` did not print JSON: {exc}"
        raise GateUnavailableError(msg) from exc
    if not isinstance(data, dict):
        msg = f"`prep_pr_state.py {label}` printed JSON that is not an object"
        raise GateUnavailableError(msg)
    return data


def _declared_autofix(entry: dict[str, Any]) -> str | None:
    """Return a gate entry's non-blank ``autofix`` string, else ``None``.

    A malformed value never raises: it means "no autofix declared".
    """
    autofix = entry.get("autofix")
    if isinstance(autofix, str) and autofix.strip():
        return autofix
    return None


def _gate_entry(entry: object) -> tuple[str, str, str | None]:
    """Return one detected gate's ``(name, command, autofix)``.

    A malformed name or command is fatal; a malformed autofix is not.
    """
    if isinstance(entry, dict):
        name = entry.get("name")
        command = entry.get("command")
        if isinstance(name, str) and name and isinstance(command, str) and command:
            return name, command, _declared_autofix(entry)
    msg = f"`prep_pr_state.py {DETECT_GATES_COMMAND}` printed a malformed gate list"
    raise GateUnavailableError(msg)


def _detected_gates(data: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """Return ``(name, command, autofix)`` per detected gate."""
    entries = data.get("gates")
    if not isinstance(entries, list):
        msg = f"`prep_pr_state.py {DETECT_GATES_COMMAND}` printed a malformed gate list"
        raise GateUnavailableError(msg)
    return [_gate_entry(entry) for entry in entries]


def _gate_ceiling(
    script: Path, name: str, *, worktree: Path, scope: GateScope
) -> float:
    """Read one gate's ``foreground_ceiling_s`` once, at cycle start."""
    label = f"{GATE_TIMEOUT_COMMAND} {name}"
    data = _json_object(
        _helper_output(
            script, (GATE_TIMEOUT_COMMAND, name), worktree=worktree, scope=scope
        ),
        label,
    )
    ceiling = data.get("foreground_ceiling_s")
    if (
        isinstance(ceiling, bool)
        or not isinstance(ceiling, (int, float))
        or not math.isfinite(ceiling)
        or ceiling <= 0
    ):
        msg = f"`prep_pr_state.py {label}` returned no positive foreground_ceiling_s"
        raise GateUnavailableError(msg)
    return ceiling


def plan_cycle_gates(worktree: Path, scope: GateScope) -> GatePlan:
    """Resolve, detect and time the repo's gates once, for one cycle.

    Call it at cycle start, on the clean tree, before the fix turn: the returned
    plan is the only input to gate execution for the cycle. Never raises; every
    failure becomes :meth:`GatePlan.unavailable`, logged once at WARNING so a
    client whose script is missing or stale leaves a trace even on cycles that
    never commit. The declared-autofix map is read once here too, and a config
    problem yields no config-declared autofix, never an unavailable plan.
    """
    try:
        script = resolve_prep_pr_state(worktree)
        detected = _detected_gates(
            _json_object(
                _helper_output(
                    script, (DETECT_GATES_COMMAND,), worktree=worktree, scope=scope
                ),
                DETECT_GATES_COMMAND,
            )
        )
        declared = declared_autofixes(worktree)
        planned = tuple(
            PlannedGate(
                name,
                command,
                _gate_ceiling(script, name, worktree=worktree, scope=scope),
                autofix or declared.get(name),
            )
            for name, command, autofix in detected
        )
    except (GateUnavailableError, OSError) as exc:
        _log.warning(
            "codex_fix_gates_unavailable ticket=%s session=%s cycle=%s why=%s",
            scope.ticket_id,
            scope.session_id,
            scope.cycle,
            exc,
        )
        return GatePlan.unavailable(str(exc))
    return GatePlan(script=script, gates=planned, why_unavailable=None)


def _postable(tail: str) -> str:
    """The last lines of gate output, secrets withheld, redacted and capped."""
    lines = tail.splitlines()[-_TAIL_LINES:]
    return redact_and_cap(
        withhold_secret_lines("\n".join(lines)),
        max_lines=_TAIL_LINES,
        max_total_chars=_TAIL_MAX_CHARS,
    )


def _run_one_gate(
    gate: PlannedGate,
    *,
    worktree: Path,
    review_artifacts: frozenset[str],
    scope: GateScope,
    tree_before: str,
) -> GateFailure | None:
    """Run one pinned gate; return why it refused the cycle, if it did.

    The gate runs in its own private scratch dir (:func:`_gate_scratch_env`); a
    scratch dir that cannot be made is ``unavailable`` like a failed start.
    """
    try:
        with _gate_scratch_env(gate.name) as env:
            watched = _run_watched(
                ["sh", "-c", gate.command],
                cwd=worktree,
                env=env,
                ceiling_s=gate.ceiling_s,
                gate=gate.name,
                scope=scope,
            )
    except OSError as exc:
        why = f"gate {gate.name} could not be started: {exc}"
        return GateFailure(
            kind="unavailable",
            gate=gate.name,
            command=gate.command,
            exit_code=None,
            seconds=0.0,
            output="",
            why=why,
        )
    _log.warning(
        "codex_fix_gate ticket=%s session=%s cycle=%s gate=%s exit=%s seconds=%.1f",
        scope.ticket_id,
        scope.session_id,
        scope.cycle,
        gate.name,
        watched.returncode,
        watched.seconds,
    )
    output = _postable(watched.tail)
    dirtied = staged_cycle_tree(worktree, review_artifacts) != tree_before
    if watched.returncode != 0:
        return GateFailure(
            kind="red",
            gate=gate.name,
            command=gate.command,
            exit_code=watched.returncode,
            seconds=watched.seconds,
            output=output,
            why=None,
        )
    if dirtied:
        return GateFailure(
            kind="dirtied",
            gate=gate.name,
            command=gate.command,
            exit_code=0,
            seconds=watched.seconds,
            output=output,
            why=None,
        )
    return None


class _Autofix(NamedTuple):
    """What :func:`_apply_autofix` did: its outcome label and the new tree."""

    outcome: str
    tree_after: str | None
    paths: int = 0


def _log_autofix(scope: GateScope, gate: PlannedGate, outcome: str, paths: int) -> None:
    _log.warning(
        "codex_fix_gate_autofix ticket=%s session=%s cycle=%s gate=%s outcome=%s "
        "paths=%d",
        scope.ticket_id,
        scope.session_id,
        scope.cycle,
        gate.name,
        outcome,
        paths,
    )


def _revert_context(scope: GateScope, gate: PlannedGate) -> str:
    """The identity a revert's log lines carry, as ``_log_autofix`` logs it."""
    return (
        f"ticket={scope.ticket_id} session={scope.session_id} "
        f"cycle={scope.cycle} gate={gate.name}"
    )


def _apply_autofix(
    gate: PlannedGate,
    command: str,
    *,
    worktree: Path,
    review_artifacts: frozenset[str],
    scope: GateScope,
    tree_before: str,
    touched: frozenset[str],
    preserve: frozenset[str],
) -> _Autofix:
    """Run *command* (the gate's declared autofix) and vet what it changed.

    ``tree_after`` is set only for outcome ``applied``; every other outcome has
    already restored the tree to *tree_before* (or never changed it). The
    autofix runs exactly as a gate does: signal-only ceiling, its own private
    scratch dir, no lock, no kill (ADR-0014, ADR-0019). An autofix that deletes
    (or renames) a path is ``reverted_scope`` like one that leaves *touched*
    (#3427). A revert
    keeps *preserve*, the ignored files on disk before the autofix ran, and
    raises ``CalledProcessError`` only when it cannot restore the tree.
    """
    context = _revert_context(scope, gate)
    if staged_cycle_tree(worktree, review_artifacts) != tree_before:
        return _Autofix("skipped_tree_changed", None)
    try:
        with _gate_scratch_env(f"{gate.name}:autofix") as env:
            watched = _run_watched(
                ["sh", "-c", command],
                cwd=worktree,
                env=env,
                ceiling_s=gate.ceiling_s,
                gate=f"{gate.name}:autofix",
                scope=scope,
            )
    except OSError:
        _log.warning("codex_fix_gate_autofix could not be started", exc_info=True)
        return _Autofix("reverted_failed", None)
    if watched.returncode != 0:
        revert_to_tree(
            worktree, review_artifacts, tree_before, preserve=preserve, context=context
        )
        return _Autofix("reverted_failed", None)
    tree_after = staged_cycle_tree(worktree, review_artifacts)
    changed = changed_paths(worktree, tree_before, tree_after)
    if not changed:
        return _Autofix("unchanged", None)
    spelled = changed_paths(worktree, tree_before, tree_after, c_quoted=True)
    deleted = changed_paths(worktree, tree_before, tree_after, diff_filter="D")
    if deleted or not spelled <= touched:
        revert_to_tree(
            worktree, review_artifacts, tree_before, preserve=preserve, context=context
        )
        return _Autofix("reverted_scope", None, len(changed))
    return _Autofix("applied", tree_after, len(changed))


def _run_gate_with_autofix(
    gate: PlannedGate,
    *,
    worktree: Path,
    review_artifacts: frozenset[str],
    scope: GateScope,
    touched: frozenset[str],
    tree_before: str,
    tokens: list[str],
) -> tuple[GateFailure | None, str]:
    """Run one gate; on a red result with a declared autofix, fix and re-check.

    Returns ``(failure, tree)`` where *tree* is the tree later gates compare
    against: *tree_before*, or the post-autofix tree after a green re-check
    (so the formatter's rewrite is not ``dirtied``). Whenever the autofix is not
    proved by a green re-check of the same gate, its changes are reverted and
    the ORIGINAL failure is returned.
    """
    failure = _run_one_gate(
        gate,
        worktree=worktree,
        review_artifacts=review_artifacts,
        scope=scope,
        tree_before=tree_before,
    )
    if failure is None or failure.kind != "red" or gate.autofix is None:
        return failure, tree_before
    preserve = ignored_on_disk(worktree)
    fixed = _apply_autofix(
        gate,
        gate.autofix,
        worktree=worktree,
        review_artifacts=review_artifacts,
        scope=scope,
        tree_before=tree_before,
        touched=touched,
        preserve=preserve,
    )
    if fixed.tree_after is None:
        _log_autofix(scope, gate, fixed.outcome, fixed.paths)
        return failure, tree_before
    recheck = _run_one_gate(
        gate,
        worktree=worktree,
        review_artifacts=review_artifacts,
        scope=scope,
        tree_before=fixed.tree_after,
    )
    if recheck is not None:
        revert_to_tree(
            worktree,
            review_artifacts,
            tree_before,
            preserve=preserve,
            context=_revert_context(scope, gate),
        )
        _log_autofix(scope, gate, "reverted_red", fixed.paths)
        return failure, tree_before
    _log_autofix(scope, gate, fixed.outcome, fixed.paths)
    if GATE_AUTOFIXED_FRICTION not in tokens:
        tokens.append(GATE_AUTOFIXED_FRICTION)
    return None, fixed.tree_after


def run_cycle_gates(
    plan: GatePlan,
    *,
    worktree: Path,
    review_artifacts: frozenset[str],
    scope: GateScope,
    tokens: list[str],
    touched: frozenset[str],
) -> GateFailure | None:
    """Run the pinned gates in order on the cycle's changes; fail fast.

    Returns ``None`` when every gate passed (or none was detected: that is
    logged at WARNING and added once to *tokens*, the run-level friction list).
    The tree is snapshotted once at gate start (post-fix, pre-commit) and
    compared after every green gate: a gate that changes it is ``dirtied``. The
    snapshot is re-taken after an applied autofix, so the formatter's rewrite is
    part of the cycle, not dirt. *touched* is the cycle's measured path set,
    the only paths an autofix may change. Only the git calls raise
    (``CalledProcessError``).
    """
    if plan.why_unavailable is not None:
        return GateFailure(
            kind="unavailable",
            gate="",
            command="",
            exit_code=None,
            seconds=0.0,
            output="",
            why=plan.why_unavailable,
        )
    if not plan.gates:
        _log.warning(
            "%s ticket=%s session=%s cycle=%s",
            NONE_DETECTED_FRICTION,
            scope.ticket_id,
            scope.session_id,
            scope.cycle,
        )
        if NONE_DETECTED_FRICTION not in tokens:
            tokens.append(NONE_DETECTED_FRICTION)
        return None
    tree = staged_cycle_tree(worktree, review_artifacts)
    for gate in plan.gates:
        failure, tree = _run_gate_with_autofix(
            gate,
            worktree=worktree,
            review_artifacts=review_artifacts,
            scope=scope,
            touched=touched,
            tree_before=tree,
            tokens=tokens,
        )
        if failure is not None:
            return failure
    return None


def gate_failure_breach(failure: GateFailure, cycle: int) -> FenceBreach:
    """Return the ``codex_fix_gate_failed`` park for a refused fix cycle."""
    if failure.kind == "unavailable":
        what = f"the repo's quality gates could not be run: {failure.why}"
        hint = (
            "Install or update prep_pr_state.py where the cycle's worktree can "
            f"find it (probed {_PROBED}), then requeue REVIEW. {LEFT_STAGED_HINT}"
        )
    else:
        if failure.kind == "red":
            what = (
                f"the repo's quality gate `{failure.gate}` failed with "
                f"exit code {failure.exit_code}"
            )
        else:
            what = (
                f"the repo's quality gate `{failure.gate}` passed but changed "
                "files (gate dirtied the worktree)"
            )
        command = redact_and_cap(
            withhold_secret_lines(failure.command),
            max_total_chars=_COMMAND_MAX_CHARS,
        )
        hint = (
            f"Run `{command}` in the worktree to see the full output, fix what "
            f"it reports, then requeue REVIEW. {LEFT_STAGED_HINT}"
        )
    details = (
        f"codex fix cycle {cycle} produced changes, but {what}. "
        "Nothing was committed or pushed."
    )
    if failure.output:
        details += (
            "\n\nLast lines of gate output (redacted, at most "
            f"{_TAIL_LINES} lines / {_TAIL_MAX_CHARS} characters):\n{failure.output}"
        )
    return FenceBreach(CODEX_FIX_GATE_FAILED, (), f"{details}\n\n{_LOG_POINTER}", hint)
