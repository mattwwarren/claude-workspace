"""The write-capable ``codex exec`` argv for the codex IMPL turn (#3061, RFC 0018 C2).

The one ``codex exec`` launch site in :mod:`cw.codex_impl`. Its sandbox and
approval policy come from the shared seam
(:func:`cw.codex_review._exec_policy_argv`) called with the caller's resolved
``fix_sandbox_mode``: the IMPL turn edits files in the ticket worktree and cw
does everything else (stage, gate, commit, push), so the mode is the lane's
``fix_sandbox_mode`` (``workspace-write``, the only value config accepts). The
package spells no sandbox-mode literal of its own.

Fixed choices:

* ``--json`` + ``--ephemeral``: the JSONL event stream (model-capacity
  detection reads it) and no resumable session file.
* ``--output-schema`` / ``-o``: the strict ``{sentinel}`` document, in a uuid
  scratch directory the driver owns.
* No ``--skip-git-repo-check``: the turn runs in the ticket worktree.
* MCP: GitHub tickets are prefetched by cw, so no MCP server is enabled;
  every declared server is disabled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from cw.codex_review import (
    _AUDIT_ARGV_FLAGS,
    _exec_policy_argv,
    _reasoning_effort_argv,
)
from cw.tracker_io._argv import _disable_others

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cw.models.enums import SandboxMode

_EXEC_PREFIX_WIDTH: Final = 2


class ImplArgvPolicyError(ValueError):
    """An IMPL argv does not carry exactly the resolved sandbox policy."""


def build_impl_argv(
    *,
    schema_path: Path,
    output_path: Path,
    model: str | None,
    reasoning_effort: str | None,
    sandbox_mode: SandboxMode,
    other_servers: Sequence[str],
) -> list[str]:
    """Return the IMPL turn's ``codex exec`` argv (the prompt goes on stdin).

    Raises :class:`~cw.tracker_io.TrackerIoConfigError` when a declared MCP
    server name cannot be disabled by a ``-c`` override.
    """
    argv = [
        "codex",
        "exec",
        *_exec_policy_argv(sandbox_mode),
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


def require_impl_sandbox_argv(argv: Sequence[str], sandbox_mode: SandboxMode) -> None:
    """Refuse any IMPL argv whose policy is not exactly the seam's for *sandbox_mode*.

    The policy block must follow ``codex exec`` contiguously and its flag must
    appear once, so neither a wider mode nor a missing approval override nor a
    second, later sandbox flag can reach the launch.
    """
    policy = tuple(_exec_policy_argv(sandbox_mode))
    start = _EXEC_PREFIX_WIDTH
    if (
        tuple(argv[start : start + len(policy)]) != policy
        or list(argv).count(policy[0]) != 1
    ):
        msg = "codex IMPL argv does not carry the resolved sandbox and approval policy"
        raise ImplArgvPolicyError(msg)
