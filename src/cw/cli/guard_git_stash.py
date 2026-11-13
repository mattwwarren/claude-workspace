"""The ``cw guard-git-stash`` PreToolUse hook handler (#1418).

The git stash stack is one ``refs/stash`` reflog shared by every worktree of a
repository, and so by every concurrent session. A dispatched worker that runs
``git stash pop`` applies and drops the *newest* entry, which may be another
session's work; a worker that stashes and never recovers silently loses its
own. Setting work aside belongs in a temporary WIP commit instead, and a stash
that cannot be avoided has a safe form (:data:`SAFE_FORM`).

The guard is the fifth command on the ``"Bash"`` PreToolUse matcher
:func:`cw.spawn._build_hook_settings` writes into every Claude-hosted worker's
``settings.local.json``, next to :mod:`cw.cli.guard_hook_install` (#3076), whose
scanner it shares (:mod:`cw.cli._shell_scan`). Codex and opencode runners write
no such file and are not covered; for them the rule is prompt text only.

Scoped to headless workers through
:func:`cw.cli._hook_io.active_headless_context`: an operator or interactive
session is never refused. Exits ``2`` with :data:`REFUSAL_REASON` on stderr;
exits ``0`` on everything else.

What is refused is every form that acts on the stack's default entry or pushes
an untagged one: a bare ``git stash`` or ``stash push`` with no message,
``save``, ``pop``, ``clear``, ``apply``/``drop`` with no stash argument,
``branch`` with no stash argument and ``store`` with no message. What stays
allowed names its target or tags its entry: ``push -m <tag>``, ``apply <sha>``,
``drop <ref>``, ``store -m <tag> <sha>``, and the read-only ``list``, ``show``
and ``create``.

Best-effort, not adversarial: the command is tokenised quote-aware, so the words
inside a quoted argument (a commit message, an ``echo``) never match, and
``bash -c``/``eval`` scripts are followed two levels deep. Not caught: a git
alias that expands to ``stash`` (``-c alias.x=stash``), backtick substitution,
and scripts that call git internally. An unquoted ``echo git stash pop`` is a
deliberate false positive, the same shape the #3076 guard accepts. An
unparseable command (an unterminated quote) gives no verdict. No toggle and no
event: the guard can only refuse a worker something it must not do.
"""

from __future__ import annotations

from typing import Final

from cw.cli._base import main
from cw.cli._hook_io import (
    _extract_bash_command,
    _read_hook_stdin_json,
    active_headless_context,
    enforce,
)
from cw.cli._lazy import GUARD_GIT_STASH_NAME
from cw.cli._shell_scan import basename, command_matches
from cw.models import BASH_TOOL_NAME

SAFE_FORM: Final = (
    'If you must stash, run `git stash push -u -m "<unique-tag>"`, capture the '
    "new entry's SHA with `git stash list --format='%H %gs'`, restore with "
    "`git stash apply <sha>` (never pop), then re-find the entry by tag and "
    "drop it."
)
REFUSAL_REASON: Final = (
    "BLOCKED (#1418): the git stash stack is shared by every worktree of this "
    "repository, so a bare `git stash`, `git stash pop`, `git stash clear` or "
    "other default-target stash use can apply or drop another session's "
    "entry, and a stash you never recover silently loses your work. Do not "
    "retry this command or another spelling of it. Prefer a temporary WIP "
    "commit to set work aside. " + SAFE_FORM + " If the command only mentions "
    "these words (a commit message or heredoc body), put the text in a file "
    "and pass it with `-F <file>`, or write it with the Write tool."
)

_GIT: Final = "git"
_STASH: Final = "stash"
# git's global options that take a separate value (``-C dir``); the ``--opt=value``
# spellings and boolean flags (``--no-pager``) are one token and need no entry.
_GIT_VALUE_OPTIONS: Final = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
        "--config-env",
        "--attr-source",
    }
)
_END_OF_OPTIONS: Final = "--"
_HELP_FLAGS: Final = frozenset({"-h", "--help"})
# Subcommands refused whatever their arguments: ``save`` is deprecated, ``pop``
# drops the entry it applies, ``clear`` wipes other sessions' entries.
_ALWAYS_REFUSED: Final = frozenset({"save", "pop", "clear"})
# Refused unless a message tags the entry that is pushed or stored.
_NEEDS_MESSAGE: Final = frozenset({"push", "store"})
# Refused unless a stash argument replaces the newest-entry default.
_NEEDS_TARGET: Final = frozenset({"apply", "drop"})
_BRANCH: Final = "branch"
# ``stash branch <name> <stash>``: without the stash it consumes the newest entry.
_BRANCH_POSITIONALS: Final = 2
_SHORT_MESSAGE_FLAG: Final = "m"
_LONG_MESSAGE_FLAG: Final = "--message"
_REDIRECT_CHARS: Final = frozenset("<>&")


def _without_redirects(tokens: list[str]) -> list[str]:
    """*tokens* minus redirections (``> file``, ``2>&1``) and their operands.

    A redirection is not an argument to git, so ``git stash > /dev/null`` is
    still a bare stash. A bare-number token before the operator is its file
    descriptor (``2 >& 1``), not an argument.
    """
    kept: list[str] = []
    skip_operand = False
    for token in tokens:
        if skip_operand:
            skip_operand = False
        elif token and set(token) <= _REDIRECT_CHARS:
            skip_operand = True
            if kept and kept[-1].isdigit():
                kept.pop()
        else:
            kept.append(token)
    return kept


def _skip_git_options(args: list[str]) -> list[str]:
    """*args* from git's subcommand on, after its global options."""
    index = 0
    while index < len(args):
        arg = args[index]
        if not arg.startswith("-"):
            return args[index:]
        index += 2 if arg in _GIT_VALUE_OPTIONS else 1
    return []


def _option_message(arg: str, following: str) -> str | None:
    """The message *arg* carries (``-m``, ``--message``, ``-um``), else None.

    *following* is the next argument, the message of a bare ``-m``/``--message``.
    """
    if arg.startswith(f"{_LONG_MESSAGE_FLAG}="):
        return arg.partition("=")[2]
    if arg == _LONG_MESSAGE_FLAG:
        return following
    if arg.startswith("-") and not arg.startswith("--"):
        cluster = arg[1:]
        flag_at = cluster.find(_SHORT_MESSAGE_FLAG)
        if flag_at != -1:
            return cluster[flag_at + 1 :] or following
    return None


def _message_given(args: list[str]) -> bool:
    """Whether *args* carry a non-empty stash message before any ``--``."""
    for index, arg in enumerate(args):
        if arg == _END_OF_OPTIONS:
            return False
        following = args[index + 1] if index + 1 < len(args) else ""
        if _option_message(arg, following):
            return True
    return False


def _positionals(args: list[str]) -> list[str]:
    return [arg for arg in args if not arg.startswith("-")]


def _subcommand_refused(subcommand: str, rest: list[str]) -> bool:
    """Whether ``git stash <subcommand> <rest>`` is an unsafe shared-stack use."""
    if subcommand in _ALWAYS_REFUSED:
        return True
    if subcommand in _NEEDS_MESSAGE:
        return not _message_given(rest)
    if subcommand in _NEEDS_TARGET:
        return not _positionals(rest)
    if subcommand == _BRANCH:
        return len(_positionals(rest)) < _BRANCH_POSITIONALS
    return False


def _stash_refused(args: list[str]) -> bool:
    """Whether ``git stash`` followed by *args* is an unsafe shared-stack use."""
    if args and args[0] in _HELP_FLAGS:
        return False
    if not args or args[0].startswith("-"):
        # No subcommand: git runs an implicit ``stash push``.
        return not _message_given(args)
    return _subcommand_refused(args[0], args[1:])


def _invokes_unsafe_stash(segment: list[str]) -> bool:
    """Whether *segment* runs a refused ``git stash`` form."""
    tokens = _without_redirects(segment)
    for index, token in enumerate(tokens):
        if basename(token) != _GIT:
            continue
        rest = _skip_git_options(tokens[index + 1 :])
        if rest and rest[0] == _STASH and _stash_refused(rest[1:]):
            return True
    return False


def _ignore_warning(_detail: str) -> None:
    """A malformed Bash payload is simply not refused; nothing to report."""


def classify_git_stash(payload: dict[str, object] | None) -> str | None:
    """:data:`REFUSAL_REASON` for a headless worker's unsafe stash, else None.

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
        refused = command_matches(command, _invokes_unsafe_stash)
    except ValueError:
        return None
    return REFUSAL_REASON if refused else None


@main.command(name=GUARD_GIT_STASH_NAME)
def guard_git_stash() -> None:
    """Refuse bare git stash, stash pop and other shared-stack stash use from a
    headless worker's Bash call.

    Reads the PreToolUse hook JSON from stdin. Exits 2 on a refusal; exits 0
    (no-op) on everything else, including a fault while reading the worker's
    context file. See :mod:`cw.cli.guard_git_stash`.
    """
    # _read_hook_stdin_json already swallows its own failures. What remains is
    # the context-file read in classify_git_stash: UnicodeDecodeError (a
    # ValueError) from undecodable bytes, ValueError from a NUL in cwd, OSError.
    # enforce stays outside the try so its exit-2 SystemExit is never swallowed.
    try:
        reason = classify_git_stash(_read_hook_stdin_json())
    except (OSError, ValueError, TypeError):
        return
    enforce(reason)
