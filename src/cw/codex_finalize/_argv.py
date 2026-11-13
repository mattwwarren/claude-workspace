"""The read-only ``codex exec`` argv for the codex FINALIZE turn (#3062, RFC 0018 C3).

The one ``codex exec`` launch site in :mod:`cw.codex_finalize`. Its sandbox
and approval policy come from the shared seam
(:func:`cw.codex_review._exec_policy_argv`) called with a fixed
``SandboxMode.READ_ONLY`` member: the FINALIZE turn writes nothing and runs
no network step (cw pushes, creates the PR, arms and verifies outside the
sandbox), so there is no knob, and ``fix_sandbox_mode`` is never read.

Fixed choices:

* ``--json`` + ``--ephemeral``: the JSONL event stream (model-capacity
  detection reads it) and no resumable session file.
* ``--output-schema`` / ``-o``: the strict four-field document, in a uuid
  scratch directory the driver owns.
* MCP: every declared server is disabled. cw supplies the ticket, the plan,
  the review artifacts and the branch facts in the prompt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from cw.codex_review import (
    _AUDIT_ARGV_FLAGS,
    _exec_policy_argv,
    _reasoning_effort_argv,
)
from cw.models.enums import SandboxMode
from cw.tracker_io._argv import _disable_others

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

#: The FINALIZE turn's sandbox and approval policy: the seam's read-only
#: output, named once. :func:`require_read_only_finalize_argv` compares a
#: launch against it.
FINALIZE_READ_ONLY_ARGV: Final[tuple[str, ...]] = tuple(
    _exec_policy_argv(SandboxMode.READ_ONLY)
)


class FinalizeArgvPolicyError(ValueError):
    """A FINALIZE argv does not carry exactly the fixed read-only policy."""


def build_finalize_argv(
    *,
    schema_path: Path,
    output_path: Path,
    model: str | None,
    reasoning_effort: str | None,
    other_servers: Sequence[str],
) -> list[str]:
    """Return the FINALIZE turn's ``codex exec`` argv (the prompt goes on stdin)."""
    argv = [
        "codex",
        "exec",
        *FINALIZE_READ_ONLY_ARGV,
        *_AUDIT_ARGV_FLAGS,
        *_reasoning_effort_argv(reasoning_effort),
        *_disable_others("", other_servers),
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
    ]
    if model:
        argv += ["-m", model]
    return argv


def require_read_only_finalize_argv(argv: Sequence[str]) -> None:
    """Refuse any argv whose policy is not exactly :data:`FINALIZE_READ_ONLY_ARGV`.

    The policy block must follow ``codex exec`` contiguously and its flag must
    appear once, so neither a wider mode nor a missing approval override nor a
    second, later sandbox flag can reach the launch.
    """
    width = len(FINALIZE_READ_ONLY_ARGV)
    if (
        tuple(argv[2 : 2 + width]) != FINALIZE_READ_ONLY_ARGV
        or list(argv).count(FINALIZE_READ_ONLY_ARGV[0]) != 1
    ):
        msg = "codex FINALIZE argv does not carry the fixed sandbox and approval policy"
        raise FinalizeArgvPolicyError(msg)
