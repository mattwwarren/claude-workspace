"""Quote-aware shell command scanning shared by the Bash PreToolUse guards.

Extracted from :mod:`cw.cli.guard_hook_install` (#3076) when
``cw guard-git-stash`` (#1418) became the second guard to need the same
tokenising and ``bash -c`` / ``eval`` descent; the same extract-on-second-
consumer move :mod:`cw.cli._hook_io` records. Duplicating it would let the two
guards drift apart.

Best-effort, not adversarial: a command is split into simple commands on shell
separators with :mod:`shlex`, so the words inside a quoted argument (a commit
message, an ``echo``) never form a command, and scripts handed to ``bash -c``
or ``eval`` are followed :data:`MAX_NESTING` levels deep. Scripts that call the
scanned binary internally, and shell constructs ``shlex`` does not model
(backtick substitution), are not seen.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable

_SHELLS: Final = frozenset({"bash", "sh", "zsh", "dash"})
_EVAL: Final = "eval"
# ``bash -c "bash -c '...'"`` is followed this many levels, then given up on.
MAX_NESTING: Final = 2
# Newline is punctuation rather than whitespace so each line is its own
# segment, and a ``#`` comment line consumes only itself.
_PUNCTUATION: Final = "();<>|&\n"
_SEPARATOR_CHARS: Final = frozenset("();|&\n")


def segments(command: str) -> list[list[str]]:
    """*command* as simple commands: token lists split on shell separators.

    Raises ``ValueError`` on an unterminated quote or escape.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace = lexer.whitespace.replace("\n", "")
    lexer.whitespace_split = True
    result: list[list[str]] = [[]]
    for token in lexer:
        if token and set(token) <= _SEPARATOR_CHARS:
            result.append([])
        else:
            result[-1].append(token)
    return [segment for segment in result if segment]


def basename(token: str) -> str:
    """The last path component of *token* (``/usr/bin/git`` -> ``git``)."""
    return token.rsplit("/", 1)[-1]


def dash_c_script(args: list[str]) -> str | None:
    """The script a shell's ``-c`` (alone or in a cluster like ``-lc``) runs."""
    for index, arg in enumerate(args):
        if not arg.startswith("-"):
            return None
        if not arg.startswith("--") and "c" in arg[1:]:
            return args[index + 1] if index + 1 < len(args) else None
    return None


def nested_scripts(segment: list[str]) -> list[str]:
    """Scripts *segment* hands to ``bash -c`` (or another shell) or ``eval``."""
    scripts: list[str] = []
    for index, token in enumerate(segment):
        name = basename(token)
        if name == _EVAL:
            scripts.append(" ".join(segment[index + 1 :]))
        elif name in _SHELLS:
            script = dash_c_script(segment[index + 1 :])
            if script is not None:
                scripts.append(script)
    return scripts


def command_matches(
    command: str, segment_predicate: Callable[[list[str]], bool], depth: int = 0
) -> bool:
    """Whether *segment_predicate* holds for a segment of *command* or of a script
    it nests (``bash -c``, ``eval``, up to :data:`MAX_NESTING` levels).

    Raises ``ValueError`` on an unterminated quote or escape.
    """
    for segment in segments(command):
        if segment_predicate(segment):
            return True
        if depth < MAX_NESTING and any(
            command_matches(script, segment_predicate, depth + 1)
            for script in nested_scripts(segment)
        ):
            return True
    return False
