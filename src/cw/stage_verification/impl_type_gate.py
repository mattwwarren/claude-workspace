"""Codex IMPL gate 4: ruff and mypy on the touched Python files (#3531).

Harness-neutral pieces of Step 2.5 gate 4 (``auto-dev-impl.md``): which pinned
gate commands declare ruff, mypy or a mypy-baseline consumer
(:func:`declares_tool`, :func:`tool_launch`), the baseline states
(:func:`detect_mypy_baseline`), the trusted-ref ``tests.prepare`` pin
(:func:`pin_prepare`), the snapshot commit and detached gate worktree the
checks run in (:func:`snapshot_commit`, :func:`gate_worktree`), the touched
file set, the baseline no-growth check, the no-baseline mypy attribution
(:func:`parse_mypy_errors`, :func:`mypy_verdict`), the ``impl_failed`` block
texts and the baselined-debt record (:func:`record_baselined_errors`).

The executor glue that runs commands lives in :mod:`cw.codex_impl._type_gate`;
this module imports no ``cw.codex_*`` module (the stage-verification import
ratchet).
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Literal

from cw._git import git_output, run_git
from cw.atomic import atomic_write_text
from cw.project_config import (
    PROJECT_CONFIG_RELPATH,
    PrepareConfigError,
    read_tests_prepare,
)
from cw.review_adjudication import (
    Adjudication,
    merge_deferred_adjudications,
    read_deferred_findings_md,
    render_deferred_findings_md,
)
from cw.review_adjudication._models import DEFERRED_OUTCOME
from cw.stage_verification.impl_block import IMPL_FAILED, ImplBlock
from cw.stage_verification.impl_gates import _capped
from cw.stage_verification.posted_text import (
    describe_added_line,
    redact_and_cap,
    withhold_secret_lines,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Iterator, Sequence

_log = logging.getLogger(__name__)

#: The ``blocker.details`` prefix of every gate 4 park.
GATE4_PREFIX: Final = "Step 2.5 gate 4 failed: "
#: The no-baseline rule, quoted exactly as ``auto-dev-impl.md`` states it.
NO_BASELINE_RULE: Final = (
    "fix every mypy error in touched files, pre-existing ones included — "
    "treat those as active bugs, not noise to inherit."
)

SKIPPED_NO_GATE_FRICTION: Final = "impl_gate4_skipped_no_gate_declared"
LAUNCHER_UNSUPPORTED_PREFIX: Final = "impl_gate4_launcher_unsupported: "
CONSUMER_UNKNOWN_PREFIX: Final = "impl_gate4_mypy_skipped_baseline_consumer_unknown: "
RUFF_NOT_DECLARED_FRICTION: Final = "impl_gate4_ruff_not_declared"
MYPY_NOT_DECLARED_FRICTION: Final = "impl_gate4_mypy_not_declared"
BASELINE_FRICTION_PREFIX: Final = "impl_mypy_baseline: "
UNTOUCHED_ONLY_PREFIX: Final = "impl_gate4_mypy_untouched_only: "
DEFERRED_UNPARSEABLE_FRICTION: Final = "deferred_findings_unparseable"
RECORD_FAILED_FRICTION: Final = "impl_baselined_record_failed"

RUFF: Final = "ruff"
MYPY: Final = "mypy"
BASELINE_TOOL: Final = "mypy-baseline"

PYTHON_SUFFIXES: Final = (".py", ".pyi")
GATE_WORKTREE_PREFIX: Final = "cw-gate-wt-impl-"
GATE_OUTPUT_PREFIX: Final = "cw-gate-out-impl-"
_GATE_ROOT_ENV: Final = "CW_GATE_ROOT"
# Off tmpfs, as the stage doc's gate fence says; spelled as gates.py spells it.
_DEFAULT_GATE_ROOT: Final = f"{os.sep}var/tmp"
_SNAPSHOT_MESSAGE: Final = "cw gate 4 snapshot"
_REGULAR_MODES: Final = frozenset({"100644", "100755"})
_SHA_PREFIX_CHARS: Final = 12
_MAX_LISTED: Final = 20
_TAIL_LINES: Final = 20
_TAIL_MAX_CHARS: Final = 1500
_COMMAND_MAX_CHARS: Final = 200
#: Mypy diagnostic lines run long (message plus code); keep them whole.
_DIAGNOSTIC_MAX_CHARS: Final = 300
_EXIT_UNTOUCHED_OK: Final = 1

_SEGMENT_SPLIT: Final = re.compile(r"&&|\|\||[;|\n]")
_WRAPPERS: Final = frozenset({"make", "pre-commit"})
# The ``uv run`` options that select the environment; cw repeats these.
_UV_FLAGS: Final = frozenset({"--all-extras", "--all-groups"})
_UV_VALUE_OPTIONS: Final = frozenset(
    {"--extra", "--group", "--with", "--python", "--env-file"}
)
_DIRECTORY_CHANGES: Final = frozenset({"cd", "pushd"})
_ASSIGNMENT: Final = re.compile(r"^[A-Za-z_]\w*\+?=")
_SHELL_EXPANSION: Final = re.compile(r"[$`]")
_CONSUMER: Final = re.compile(r"mypy-baseline\s+filter|--baseline-file")
_BASELINE_PATH_ARG: Final = re.compile(r"--baseline-(?:file|path)(?:=|\s+)(\S+)")
_DEFAULT_BASELINE_FILES: Final = ("mypy-baseline.txt", ".mypy/baseline.json")
_ADDED_LINE: Final = re.compile(r"^\+[^+]")
_DIAGNOSTIC: Final = re.compile(
    r"^(?P<path>[^\s:][^:]*\.pyi?):(?:(?P<line>\d+):(?:\d+:)*)? error: (?P<msg>.*)$"
)
_ERROR_CODE: Final = re.compile(r"\s\[(?P<code>[a-z0-9-]+)\]\s*$")


# -- command declaration ---------------------------------------------------------


@dataclass(frozen=True)
class UvRun:
    """A tool a declaring segment runs through ``uv run``.

    ``options`` are the environment options cw repeats on its own command for
    the tool; ``None`` means cw cannot reproduce the declaring command.
    """

    options: tuple[str, ...] | None


#: How a declaring segment launches a tool: through ``uv run``, or otherwise.
Launch = UvRun | Literal["other"]


def _tool_pattern(tool: str) -> re.Pattern[str]:
    # Not part of a longer token: `mypy-baseline` and `.mypy/` are not `mypy`.
    return re.compile(rf"(?<![\w.-]){re.escape(tool)}(?![\w./-])")


def _segments(command: str) -> list[str]:
    return [part.strip() for part in _SEGMENT_SPLIT.split(command) if part.strip()]


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _is_wrapper(tokens: Sequence[str]) -> bool:
    return any(PurePosixPath(token).name in _WRAPPERS for token in tokens)


def _changes_shell(tokens: Sequence[str]) -> bool:
    """Whether a segment hands later ones another directory or environment."""
    words = [word for token in tokens if (word := token.lstrip("({\"'"))]
    if not words:
        return False
    return (
        words[0] == "export"
        or any(word in _DIRECTORY_CHANGES for word in words)
        or all(_ASSIGNMENT.match(word) for word in words)
    )


def _declaring_segments(command: str, tool: str) -> list[tuple[list[str], bool]]:
    """The segments that name *tool* and are not a wrapper run.

    Each is its tokens and whether an earlier segment changed the directory or
    the environment.
    """
    pattern = _tool_pattern(tool)
    found: list[tuple[list[str], bool]] = []
    changed = False
    for segment in _segments(command):
        tokens = _tokens(segment)
        if pattern.search(segment) and not _is_wrapper(tokens):
            found.append((tokens, changed))
        changed = changed or _changes_shell(tokens)
    return found


def declares_tool(command: str, tool: str) -> bool:
    """Whether a segment of *command* names *tool* outside ``make``/``pre-commit``."""
    return bool(_declaring_segments(command, tool))


def _uv_run_at(tokens: Sequence[str], tool: str) -> int | None:
    """Where the ``uv run`` before the first token naming *tool* starts."""
    pattern = _tool_pattern(tool)
    for index, token in enumerate(tokens):
        if pattern.search(token):
            return next(
                (
                    at
                    for at in range(index - 1)
                    if PurePosixPath(tokens[at]).name == "uv"
                    and tokens[at + 1] == "run"
                ),
                None,
            )
    return None


def _env_options(args: Sequence[str]) -> tuple[str, ...] | None:
    """The environment options leading ``uv run``'s *args*.

    ``None`` when an option is not one cw repeats (``--directory``,
    ``--project``, ``--package`` or anything unrecognised), or when its value is
    missing or needs shell expansion.
    """
    options: list[str] = []
    rest = iter(args)
    for arg in rest:
        if not arg.startswith("-"):
            break
        name, assigned, value = arg.partition("=")
        if name in _UV_FLAGS and not assigned:
            options.append(arg)
            continue
        if name not in _UV_VALUE_OPTIONS:
            return None
        options.append(arg)
        if not assigned:
            value = next(rest, "")
            options.append(value)
        if not value or _SHELL_EXPANSION.search(value):
            return None
    return tuple(options)


def _segment_launch(tokens: Sequence[str], tool: str, *, changed: bool) -> Launch:
    at = _uv_run_at(tokens, tool)
    if at is None:
        return "other"
    if changed or any(_ASSIGNMENT.match(token) for token in tokens[:at]):
        return UvRun(options=None)
    return UvRun(options=_env_options(tokens[at + 2 :]))


def _rank(launch: Launch) -> int:
    if not isinstance(launch, UvRun):
        return 2
    return 1 if launch.options is None else 0


def _preferred(launches: Iterable[Launch | None]) -> Launch | None:
    """A reproducible ``uv run`` first, then any ``uv run``, then ``other``."""
    ranked = sorted((item for item in launches if item is not None), key=_rank)
    return ranked[0] if ranked else None


def tool_launch(command: str, tool: str) -> Launch | None:
    """:class:`UvRun` when a declaring segment runs *tool* through ``uv run``.

    ``other`` when *tool* is declared only some other way (bare, ``uvx``,
    ``poetry run``), ``None`` when *command* does not declare it.
    """
    return _preferred(
        _segment_launch(tokens, tool, changed=changed)
        for tokens, changed in _declaring_segments(command, tool)
    )


def launch_over(commands: Sequence[str], tool: str) -> Launch | None:
    """The preferred :func:`tool_launch` over every pinned command."""
    return _preferred(tool_launch(command, tool) for command in commands)


def consumer_launch(commands: Sequence[str]) -> Launch | None:
    """The launch of the baseline consumers: the mypy segment, else the consumer's."""
    consumers = [command for command in commands if is_baseline_consumer(command)]
    return launch_over(consumers, MYPY) or launch_over(consumers, BASELINE_TOOL)


# -- baseline detection ------------------------------------------------------------


def is_baseline_consumer(command: str) -> bool:
    """Whether *command* consumes a mypy baseline (``mypy-baseline filter``)."""
    return bool(_CONSUMER.search(command))


def baseline_candidates(commands: Sequence[str]) -> tuple[str, ...]:
    """The default baseline paths plus every ``--baseline-file``/``-path`` argument."""
    found = list(_DEFAULT_BASELINE_FILES)
    for command in commands:
        for match in _BASELINE_PATH_ARG.finditer(command):
            path = os.path.normpath(match.group(1).strip("'\""))
            if path not in found:
                found.append(path)
    return tuple(found)


@dataclass(frozen=True)
class MypyBaseline:
    """A tracked mypy baseline: its files and the pinned commands consuming it.

    Empty ``commands`` is the consumer-unknown state: a candidate is tracked
    but no pinned command visibly consumes it.
    """

    files: tuple[str, ...]
    commands: tuple[str, ...]


def detect_mypy_baseline(
    worktree: Path, fork: str, commands: Sequence[str]
) -> MypyBaseline | None:
    """The baseline at *fork*, or ``None`` when no candidate file is tracked."""
    candidates = baseline_candidates(commands)
    listed = git_output(
        ["ls-tree", "-r", "--name-only", fork, "--", *candidates], cwd=worktree
    )
    tracked = set(listed.splitlines())
    files = tuple(path for path in candidates if path in tracked)
    if not files:
        return None
    consumers = tuple(command for command in commands if is_baseline_consumer(command))
    return MypyBaseline(files=files, commands=consumers)


# -- tests.prepare from the default branch ---------------------------------------


@dataclass(frozen=True)
class PreparePin:
    """The default branch's ``tests.prepare``: a command, an error, or neither."""

    command: str | None = None
    error: str | None = None


def _prepare_ref(worktree: Path, default_branch: str) -> str:
    """``origin/HEAD``'s target, else ``origin/<default_branch>``."""
    head = run_git(
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        cwd=worktree,
        capture_output=True,
    )
    target = head.stdout.strip() if head.returncode == 0 else ""
    return target or f"origin/{default_branch}"


def _pinned_text(worktree: Path, ref: str) -> str | PreparePin:
    """The committed config text at *ref*, or the pin (unset or an error)."""
    config = PROJECT_CONFIG_RELPATH.as_posix()
    listed = run_git(["ls-tree", ref, "--", config], cwd=worktree, capture_output=True)
    if listed.returncode != 0:
        return PreparePin(error=f"the default-branch ref {ref} could not be read")
    entry = listed.stdout.strip()
    if not entry:
        return PreparePin()
    mode = entry.split(maxsplit=1)[0]
    if mode not in _REGULAR_MODES:
        return PreparePin(error=f"{config} at {ref} is not a regular file")
    return git_output(
        ["show", f"{ref}:{config}"], cwd=worktree, errors="surrogateescape"
    )


def pin_prepare(worktree: Path, default_branch: str) -> PreparePin:
    """Read ``tests.prepare`` from the default branch, never from the worktree.

    The ref is ``origin/HEAD``'s target, else ``origin/<default_branch>``. The
    committed file is materialised in a temporary directory and read by
    :func:`cw.project_config.read_tests_prepare`. Never raises.
    """
    try:
        ref = _prepare_ref(worktree, default_branch)
        text = _pinned_text(worktree, ref)
        if isinstance(text, PreparePin):
            return text
        with tempfile.TemporaryDirectory(prefix="cw-prepare-pin-") as scratch:
            path = Path(scratch) / PROJECT_CONFIG_RELPATH
            path.parent.mkdir(parents=True)
            path.write_text(text, encoding="utf-8", errors="surrogateescape")
            return PreparePin(command=read_tests_prepare(Path(scratch)))
    except (subprocess.CalledProcessError, OSError, PrepareConfigError) as exc:
        return PreparePin(error=why_of(exc))


# -- the snapshot, the fork and the touched files --------------------------------


def why_of(exc: BaseException) -> str:
    """A short reason for *exc*: a git failure's stderr, else its message."""
    if isinstance(exc, subprocess.CalledProcessError):
        stream = exc.stderr or exc.stdout or ""
        text = stream if isinstance(stream, str) else stream.decode(errors="replace")
        if text.strip():
            return text.strip()
    return str(exc)


def snapshot_commit(worktree: Path, tree: str) -> str:
    """A dangling commit of *tree* on ``HEAD`` (and ``MERGE_HEAD``); no ref moves."""
    parents = ["-p", "HEAD"]
    merge = run_git(
        ["rev-parse", "--quiet", "--verify", "MERGE_HEAD"],
        cwd=worktree,
        capture_output=True,
    )
    if merge.returncode == 0 and merge.stdout.strip():
        parents += ["-p", merge.stdout.strip()]
    return git_output(
        ["commit-tree", tree, *parents, "-m", _SNAPSHOT_MESSAGE], cwd=worktree
    ).strip()


def gate_fork_point(worktree: Path, base_branch: str, snap: str) -> str:
    """``git merge-base origin/<base_branch> <snap>``."""
    return git_output(
        ["merge-base", f"origin/{base_branch}", snap], cwd=worktree
    ).strip()


def is_python(path: str) -> bool:
    """Whether *path* is a ``.py`` or ``.pyi`` file."""
    return path.endswith(PYTHON_SUFFIXES)


def touched_python_files(worktree: Path, fork: str, snap: str) -> tuple[str, ...]:
    """The ``.py``/``.pyi`` files the branch changes since *fork*, deletions aside."""
    out = git_output(
        ["diff", "-z", "--name-only", "--no-renames", "--diff-filter=d", fork, snap],
        cwd=worktree,
        errors="surrogateescape",
    )
    return tuple(sorted(name for name in out.split("\0") if name and is_python(name)))


def path_args(files: Sequence[str]) -> list[str]:
    """*files* as command arguments: a leading ``-`` gets a ``./`` prefix."""
    return [f"./{name}" if name.startswith("-") else name for name in files]


def baseline_growth(worktree: Path, fork: str, snap: str, path: str) -> list[str]:
    """The lines the branch added to baseline file *path* since *fork*."""
    out = git_output(["diff", fork, snap, "--", path], cwd=worktree)
    return [line[1:] for line in out.splitlines() if _ADDED_LINE.match(line)]


# -- the gate worktree ------------------------------------------------------------


def gate_root() -> str:
    """``CW_GATE_ROOT``, else ``/var/tmp``."""
    return os.environ.get(_GATE_ROOT_ENV) or _DEFAULT_GATE_ROOT


def _remove_dir(path: str, ticket: str, what: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
    if Path(path).exists():
        _log.warning("%s ticket=%s path=%s", what, ticket, path)


@contextmanager
def gate_worktree(
    worktree: Path, snap: str, *, root: str, ticket: str
) -> Iterator[Path]:
    """A detached temporary worktree at *snap*, removed on every exit.

    Hooks are off for the add. A failed add raises ``CalledProcessError``; a
    directory left behind logs WARNING ``impl_gate4_worktree_leftover``.
    """
    path = tempfile.mkdtemp(prefix=GATE_WORKTREE_PREFIX, dir=root)
    try:
        run_git(
            [
                "-c",
                "core.hooksPath=/dev/null",
                "worktree",
                "add",
                "--detach",
                path,
                snap,
            ],
            cwd=worktree,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        shutil.rmtree(path, ignore_errors=True)
        raise
    try:
        yield Path(path)
    finally:
        # Removing the directory and pruning is ``worktree remove --force``.
        _remove_dir(path, ticket, "impl_gate4_worktree_leftover")
        run_git(["worktree", "prune"], cwd=worktree, capture_output=True)


@contextmanager
def gate_output_dir(root: str, ticket: str) -> Iterator[Path]:
    """A private directory for captured command output, removed on every exit."""
    path = tempfile.mkdtemp(prefix=GATE_OUTPUT_PREFIX, dir=root)
    try:
        yield Path(path)
    finally:
        _remove_dir(path, ticket, "impl_gate4_worktree_leftover")


# -- mypy attribution ---------------------------------------------------------------


@dataclass(frozen=True)
class MypyError:
    """One mypy ``error:`` diagnostic: normalized path, line, code and the line."""

    path: str
    line: int | None
    code: str | None
    text: str


def _normalized(path: str, root: Path | None) -> str:
    if root is not None and Path(path).is_absolute():
        try:
            path = os.path.relpath(path, root)
        except ValueError:
            return os.path.normpath(path)
    return os.path.normpath(path)


def parse_mypy_errors(text: str, root: Path | None = None) -> list[MypyError]:
    """Every ``<path>.py[i]:[<line>:...] error: ...`` line of mypy output *text*.

    ``note:`` lines, ``mypy: error: ...``, config messages and ``uv`` lines are
    not diagnostics. An absolute path under *root* is made relative.
    """
    errors: list[MypyError] = []
    for line in text.splitlines():
        match = _DIAGNOSTIC.match(line)
        if match is None:
            continue
        code = _ERROR_CODE.search(match.group("msg"))
        number = match.group("line")
        errors.append(
            MypyError(
                path=_normalized(match.group("path"), root),
                line=int(number) if number is not None else None,
                code=code.group("code") if code is not None else None,
                text=line,
            )
        )
    return errors


class VerdictKind(StrEnum):
    """How a no-baseline mypy run disposes."""

    PASSED = "passed"
    TOUCHED = "touched"
    UNTOUCHED_ONLY = "untouched_only"
    UNPARSED = "unparsed"


@dataclass(frozen=True)
class MypyVerdict:
    """The disposition, the touched-file errors and the untouched files."""

    kind: VerdictKind
    touched_errors: tuple[MypyError, ...] = ()
    untouched_files: tuple[str, ...] = ()


def mypy_verdict(
    exit_code: int, errors: Sequence[MypyError], touched: Collection[str]
) -> MypyVerdict:
    """Block by path, never by a fork-point count delta.

    Exit 0 passes. Any diagnostic in a touched file blocks. Exit 1 whose every
    diagnostic is in an untouched file passes (``untouched_only``). Anything
    else non-zero blocks (exit 2 can stop mypy before the touched files).
    """
    if exit_code == 0:
        return MypyVerdict(VerdictKind.PASSED)
    wanted = {os.path.normpath(path) for path in touched}
    mine = tuple(error for error in errors if error.path in wanted)
    if mine:
        return MypyVerdict(VerdictKind.TOUCHED, touched_errors=mine)
    if exit_code == _EXIT_UNTOUCHED_OK and errors:
        others = tuple(sorted({error.path for error in errors}))
        return MypyVerdict(VerdictKind.UNTOUCHED_ONLY, untouched_files=others)
    return MypyVerdict(VerdictKind.UNPARSED)


def listed_files(files: Sequence[str]) -> str:
    """A redacted, capped, comma-joined file list for friction and log lines."""
    shown = ", ".join(files[:_MAX_LISTED])
    if len(files) > _MAX_LISTED:
        shown += f", ... and {len(files) - _MAX_LISTED} more"
    return redact_and_cap(shown)


# -- block texts ----------------------------------------------------------------------


def gate4_block(body: str) -> ImplBlock:
    """An ``impl_failed`` park whose details start ``Step 2.5 gate 4 failed:``."""
    return ImplBlock(IMPL_FAILED, f"{GATE4_PREFIX}{body}")


def postable_command(command: str) -> str:
    """*command* for a park text: secrets withheld, redacted and capped."""
    return redact_and_cap(
        withhold_secret_lines(command), max_total_chars=_COMMAND_MAX_CHARS
    )


def postable_output(output: str) -> str:
    """The last lines of *output*: secrets withheld, redacted and capped."""
    lines = output.splitlines()[-_TAIL_LINES:]
    return redact_and_cap(
        withhold_secret_lines("\n".join(lines)),
        max_lines=_TAIL_LINES,
        max_total_chars=_TAIL_MAX_CHARS,
    )


def _scope_lines(files: Sequence[str]) -> str:
    listed = [redact_and_cap(name) for name in files]
    return "Touched files the re-run covered:\n" + "\n".join(_capped(listed))


def setup_block(why: str) -> ImplBlock:
    """The gate's own setup failed (snapshot, fork, worktree or scratch dir)."""
    return gate4_block(f"gate setup: {redact_and_cap(why)}")


def unavailable_block(command: str, why: str) -> ImplBlock:
    """A command that could not start, or had no scratch dir."""
    return gate4_block(
        f"`{postable_command(command)}` could not be run: {redact_and_cap(why)}"
    )


def prepare_unreadable_block(why: str) -> ImplBlock:
    """The default branch's ``tests.prepare`` could not be read."""
    return gate4_block(f"tests.prepare could not be read: {redact_and_cap(why)}")


def prepare_unavailable_block(why: str) -> ImplBlock:
    """``tests.prepare`` could not be started."""
    return gate4_block(f"tests.prepare could not be run: {redact_and_cap(why)}")


def prepare_failed_block(command: str, exit_code: int, output: str) -> ImplBlock:
    """``tests.prepare`` exited non-zero; no check ran."""
    return gate4_block(
        f"tests.prepare `{postable_command(command)}` exited {exit_code} before "
        f"the type check ran.\n\n{postable_output(output)}"
    )


def ruff_block(
    command: str, exit_code: int, output: str, files: Sequence[str]
) -> ImplBlock:
    """``ruff check`` exited non-zero on the touched files."""
    return gate4_block(
        f"`{postable_command(command)}` exited {exit_code}.\n\n"
        f"{postable_output(output)}\n\n{_scope_lines(files)}"
    )


def mypy_touched_block(
    command: str,
    exit_code: int,
    errors: Sequence[MypyError],
    files: Sequence[str],
) -> ImplBlock:
    """Mypy reported errors in touched files: those lines, the rule, the scope."""
    lines = [
        redact_and_cap(
            withhold_secret_lines(error.text), max_line_chars=_DIAGNOSTIC_MAX_CHARS
        )
        for error in errors[:_MAX_LISTED]
    ]
    if len(errors) > _MAX_LISTED:
        lines.append(f"... and {len(errors) - _MAX_LISTED} more")
    return gate4_block(
        f"`{postable_command(command)}` exited {exit_code}.\n\n"
        + "\n".join(lines)
        + f'\n\nThe rule: "{NO_BASELINE_RULE}"\n\n{_scope_lines(files)}'
    )


def mypy_failed_block(
    command: str, exit_code: int, output: str, files: Sequence[str]
) -> ImplBlock:
    """Mypy failed with no touched-file diagnostic (unparsed, or exit 2+)."""
    return gate4_block(
        f"`{postable_command(command)}` exited {exit_code}.\n\n"
        f"{postable_output(output)}\n\n"
        f'The rule: "{NO_BASELINE_RULE}"\n\n{_scope_lines(files)}'
    )


def baseline_rule(command: str, fork: str) -> str:
    """The baseline branch's rule, naming the command and the fork sha."""
    return (
        f"the blocking set is what `{postable_command(command)}` reports in "
        "touched files, and the baseline file may not gain entries since "
        f"{fork[:_SHA_PREFIX_CHARS]}."
    )


def baseline_command_block(
    command: str, exit_code: int, output: str, fork: str, files: Sequence[str]
) -> ImplBlock:
    """A baseline-consuming mypy command exited non-zero."""
    return gate4_block(
        f"`{postable_command(command)}` exited {exit_code}.\n\n"
        f"{postable_output(output)}\n\nThe rule: {baseline_rule(command, fork)}"
        f"\n\n{_scope_lines(files)}"
    )


def growth_block(path: str, added: Sequence[str]) -> ImplBlock:
    """The baseline file gained entries: a suppression needing explicit approval."""
    shown = [describe_added_line(line) for line in added]
    return gate4_block(
        f"the mypy baseline file {redact_and_cap(path)} gained entries in this "
        "diff, a suppression that needs explicit approval:\n"
        + "\n".join(_capped(shown))
    )


# -- the baselined-debt record ----------------------------------------------------


def _rationale(path: str, errors: Sequence[MypyError]) -> str:
    entries = []
    for error in errors[:_MAX_LISTED]:
        where = f"{path}:{error.line}" if error.line is not None else path
        entries.append(f"{where} [{error.code}]" if error.code else where)
    if len(errors) > _MAX_LISTED:
        entries.append(f"and {len(errors) - _MAX_LISTED} more")
    text = (
        "baselined debt in a file this change touched; not introduced by it: "
        + ", ".join(entries)
    )
    return text.replace('"', "")


def _candidates(
    errors: Sequence[MypyError], touched: Collection[str]
) -> list[Adjudication]:
    by_file: dict[str, list[MypyError]] = {}
    for error in errors:
        if error.path in touched:
            by_file.setdefault(error.path, []).append(error)
    return [
        Adjudication(
            severity="SHOULD_FIX",
            file=path,
            summary=f"pre-existing baselined mypy errors in {path}".replace('"', ""),
            outcome=DEFERRED_OUTCOME,
            rationale=_rationale(path, found),
        )
        for path, found in sorted(by_file.items())
    ]


def record_baselined_errors(
    path: Path, errors: Sequence[MypyError], touched: Collection[str]
) -> list[str]:
    """Merge one entry per touched file with baselined errors; return friction.

    A file that already has an entry is skipped (an explicit filter: the merge
    dedupes on full content). A malformed artifact is left untouched with
    ``deferred_findings_unparseable``; any other failure is
    ``impl_baselined_record_failed``. Never blocks.
    """
    candidates = _candidates(errors, touched)
    if not candidates:
        return []
    try:
        prior = read_deferred_findings_md(path)
    except ValueError:
        return [DEFERRED_UNPARSEABLE_FRICTION]
    except OSError:
        return [RECORD_FAILED_FRICTION]
    seen = {entry.file for entry in prior}
    remaining = [entry for entry in candidates if entry.file not in seen]
    if not remaining:
        return []
    try:
        rendered = render_deferred_findings_md(
            merge_deferred_adjudications(prior, remaining)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, rendered)
    except (OSError, ValueError):
        return [RECORD_FAILED_FRICTION]
    return []


__all__ = [
    "BASELINE_FRICTION_PREFIX",
    "BASELINE_TOOL",
    "CONSUMER_UNKNOWN_PREFIX",
    "DEFERRED_UNPARSEABLE_FRICTION",
    "GATE4_PREFIX",
    "LAUNCHER_UNSUPPORTED_PREFIX",
    "MYPY",
    "MYPY_NOT_DECLARED_FRICTION",
    "NO_BASELINE_RULE",
    "RECORD_FAILED_FRICTION",
    "RUFF",
    "RUFF_NOT_DECLARED_FRICTION",
    "SKIPPED_NO_GATE_FRICTION",
    "UNTOUCHED_ONLY_PREFIX",
    "Launch",
    "MypyBaseline",
    "MypyError",
    "MypyVerdict",
    "PreparePin",
    "UvRun",
    "VerdictKind",
    "baseline_candidates",
    "baseline_command_block",
    "baseline_growth",
    "baseline_rule",
    "consumer_launch",
    "declares_tool",
    "detect_mypy_baseline",
    "gate4_block",
    "gate_fork_point",
    "gate_output_dir",
    "gate_root",
    "gate_worktree",
    "growth_block",
    "is_baseline_consumer",
    "is_python",
    "launch_over",
    "listed_files",
    "mypy_failed_block",
    "mypy_touched_block",
    "mypy_verdict",
    "parse_mypy_errors",
    "path_args",
    "pin_prepare",
    "postable_command",
    "postable_output",
    "prepare_failed_block",
    "prepare_unavailable_block",
    "prepare_unreadable_block",
    "record_baselined_errors",
    "ruff_block",
    "setup_block",
    "snapshot_commit",
    "tool_launch",
    "touched_python_files",
    "unavailable_block",
    "why_of",
]
