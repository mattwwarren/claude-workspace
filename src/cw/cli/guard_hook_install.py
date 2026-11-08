"""The ``cw guard-hook-install`` PreToolUse hook handler (#3076).

``pre-commit install`` and ``pre-commit uninstall`` rewrite the ``.git/hooks``
directory that every worktree of a repository shares, and the hook they write
pins ``INSTALL_PYTHON`` to the venv of whichever checkout ran them. Run from a
dispatched worker, that is the worker's own venv, deleted when the worktree is
cleaned up, and every other worktree's git runs the worker's hook until then.

``cw`` cannot guard a third-party binary from inside its own CLI
(:func:`cw.cli._worker_guard.refuse_inside_dispatch_worker` covers ``cw``
subcommands only), so the guard is the fourth command on the ``"Bash"``
PreToolUse matcher :func:`cw.spawn._build_hook_settings` writes into every
Claude-hosted worker's ``settings.local.json``. Codex and opencode runners
write no such file and are not covered.

Scoped to headless workers through
:func:`cw.cli._hook_io.active_headless_context`, the same positive-evidence
rule ``refuse_inside_dispatch_worker`` applies: an operator or interactive
session is never refused. Exits ``2`` with :data:`REFUSAL_REASON` on stderr;
exits ``0`` on everything else.

Best-effort, not adversarial: the command is tokenised quote-aware, so the
words inside a quoted argument (a commit message, an ``echo``) never match, and
``bash -c``/``eval`` scripts are followed two levels deep. Indirect scripts
that call ``pre-commit`` internally are not caught. No toggle and no event:
the guard can only refuse a worker something it must not do.
"""

from __future__ import annotations

import shlex
from typing import Final

from cw.cli._base import main
from cw.cli._hook_io import (
    _extract_bash_command,
    _read_hook_stdin_json,
    active_headless_context,
    enforce,
)
from cw.models import BASH_TOOL_NAME

OPERATOR_COMMAND: Final = "uv run pre-commit install --hook-type pre-commit"
OPERATOR_ACTION: Final = (
    f"Operator action: run `{OPERATOR_COMMAND}` from the main checkout "
    "(`cw doctor` reports a missing or stale hook as `commit-hook/<client>` "
    "and a hook pinned to a worker venv as `hook-install-python/<client>`)."
)
REFUSAL_REASON: Final = (
    "BLOCKED (#3076): `pre-commit install` and `pre-commit uninstall` rewrite "
    "the .git/hooks directory that every worktree of this repository shares, "
    "and the installed hook pins INSTALL_PYTHON to this worker's venv, which is "
    "deleted when the worktree is cleaned up. Do not retry this command or "
    "another spelling of it. "
    + OPERATOR_ACTION
    + " Record the missing or stale hook as friction in your result instead of "
    "repairing it."
)

_PRE_COMMIT_NAMES: Final = frozenset({"pre-commit", "pre_commit"})
_REFUSED_SUBCOMMANDS: Final = frozenset({"install", "uninstall"})
# pre-commit's global options that take a separate value (``-c cfg.yaml``).
_VALUE_OPTIONS: Final = frozenset({"-c", "--config", "--color"})
_SHELLS: Final = frozenset({"bash", "sh", "zsh", "dash"})
_EVAL: Final = "eval"
# ``bash -c "bash -c '...'"`` is followed this many levels, then given up on.
_MAX_NESTING: Final = 2
# Newline is punctuation rather than whitespace so each line is its own
# segment, and a ``#`` comment line consumes only itself.
_PUNCTUATION: Final = "();<>|&\n"
_SEPARATOR_CHARS: Final = frozenset("();|&\n")


def _segments(command: str) -> list[list[str]]:
    """*command* as simple commands: token lists split on shell separators.

    Raises ``ValueError`` on an unterminated quote or escape.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace = lexer.whitespace.replace("\n", "")
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token and set(token) <= _SEPARATOR_CHARS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _first_positional(args: list[str]) -> str | None:
    """The first argument that is neither an option nor an option's value."""
    skip_value = False
    for arg in args:
        if skip_value:
            skip_value = False
            continue
        if arg.startswith("-"):
            skip_value = arg in _VALUE_OPTIONS
            continue
        return arg
    return None


def _invokes_hook_install(segment: list[str]) -> bool:
    """Whether *segment* runs ``pre-commit install`` or ``pre-commit uninstall``."""
    return any(
        _basename(token) in _PRE_COMMIT_NAMES
        and _first_positional(segment[index + 1 :]) in _REFUSED_SUBCOMMANDS
        for index, token in enumerate(segment)
    )


def _dash_c_script(args: list[str]) -> str | None:
    """The script a shell's ``-c`` (alone or in a cluster like ``-lc``) runs."""
    for index, arg in enumerate(args):
        if not arg.startswith("-"):
            return None
        if not arg.startswith("--") and "c" in arg[1:]:
            return args[index + 1] if index + 1 < len(args) else None
    return None


def _nested_scripts(segment: list[str]) -> list[str]:
    """Scripts *segment* hands to ``bash -c`` (or another shell) or ``eval``."""
    scripts: list[str] = []
    for index, token in enumerate(segment):
        name = _basename(token)
        if name == _EVAL:
            scripts.append(" ".join(segment[index + 1 :]))
        elif name in _SHELLS:
            script = _dash_c_script(segment[index + 1 :])
            if script is not None:
                scripts.append(script)
    return scripts


def _command_installs_hooks(command: str, depth: int = 0) -> bool:
    """Whether *command*, or a script it nests, runs a hook install/uninstall."""
    for segment in _segments(command):
        if _invokes_hook_install(segment):
            return True
        if depth < _MAX_NESTING and any(
            _command_installs_hooks(script, depth + 1)
            for script in _nested_scripts(segment)
        ):
            return True
    return False


def _ignore_warning(_detail: str) -> None:
    """A malformed Bash payload is simply not refused; nothing to report."""


def classify_hook_install(payload: dict[str, object] | None) -> str | None:
    """:data:`REFUSAL_REASON` for a headless worker's hook install, else None.

    Total over the payload's shape: a missing payload, another tool, a
    non-dict ``tool_input``, a non-string or missing ``command`` and an
    unparseable command (unterminated quote) all give no verdict. It does not
    guard the context-file read inside :func:`active_headless_context`.
    """
    if payload is None or payload.get("tool_name") != BASH_TOOL_NAME:
        return None
    if active_headless_context(payload) is None:
        return None
    command, _run_in_background = _extract_bash_command(payload, _ignore_warning)
    if command is None:
        return None
    try:
        refused = _command_installs_hooks(command)
    except ValueError:
        return None
    return REFUSAL_REASON if refused else None


@main.command(name="guard-hook-install")
def guard_hook_install() -> None:
    """Refuse pre-commit install or uninstall from a headless worker's Bash call.

    Reads the PreToolUse hook JSON from stdin. Exits 2 on a refusal; exits 0
    (no-op) on everything else, including a fault while reading the worker's
    context file. See :mod:`cw.cli.guard_hook_install`.
    """
    # _read_hook_stdin_json already swallows its own failures. What remains is
    # the context-file read in classify_hook_install: UnicodeDecodeError (a
    # ValueError) from undecodable bytes, ValueError from a NUL in cwd, OSError.
    # enforce stays outside the try so its exit-2 SystemExit is never swallowed.
    try:
        reason = classify_hook_install(_read_hook_stdin_json())
    except (OSError, ValueError, TypeError):
        return
    enforce(reason)
