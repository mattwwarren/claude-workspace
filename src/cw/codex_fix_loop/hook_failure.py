"""Commit-hook failures inside a codex fix cycle (#2633).

A fix cycle whose ``git commit`` a repo hook (pre-commit, commit-msg) rejects
after the one re-stage retry used to park as a bare ``codex_error`` with the
hook's output lost to the driver log. It now parks ``codex_fix_hook_failed``
with a bounded excerpt. Hook output can echo secrets, so the excerpt is never
posted verbatim: secret-looking lines are withheld, the rest is redacted and
capped (20 lines, 1,000 characters) through :mod:`.posted_text`, and a
secret-scanning hook's output is not posted at all (name and exit code only).
The full output goes, uncapped, to the WARNING log, which in production is
``.cw/codex_driver.log`` in the cycle worktree (the detached ``cw codex run``
job's stdout and stderr) and never reaches the tracker.

A content failure (an ordinary lint error naming a measured path) earns the
fix cycle one bounded repair turn (#2987); :func:`repair_excerpt` decides
which failures qualify, fail-closed. Everything else parks as before.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import TYPE_CHECKING, Literal

from cw._git import git_hooks_dir
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT, FenceBreach
from cw.codex_fix_loop.posted_text import redact_and_cap, withhold_secret_lines
from cw.codex_review import CODEX_FIX_HOOK_FAILED

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_log = logging.getLogger(__name__)

_HOOK_NAMES = ("pre-commit", "commit-msg")
_HOOK_EXCERPT_MAX_LINES = 20
_HOOK_EXCERPT_MAX_CHARS = 1000
_HOOK_ID_MAX_CHARS = 80
_HOOK_ID = re.compile(r"^- hook id: (\S+)\s*$", re.MULTILINE)
_EXIT_CODE = re.compile(r"^- exit code: (\d+)\s*$", re.MULTILINE)
_SECRET_SCANNER = re.compile(
    r"(?i)gitleaks|detect-secrets|detect_secrets|trufflehog|secretlint|git-secrets"
    r"|ggshield|detect-private-key|detect-aws-credentials"
    r"|(?:secret|credential|token|password|api[-_ ]?key)s?[-_ ]?"
    r"(?:scan|check|detect|lint|leak)"
)
_LOG_POINTER = (
    "The full hook output is in .cw/codex_driver.log in the cycle worktree "
    "(WARNING from logger cw.codex_fix_loop.hook_failure); re-running "
    "`git commit` in the worktree shows it too."
)
# Non-content failures: the hook could not run, not that the staged change is
# wrong. A repair turn would only waste a fix invocation on these.
_ENVIRONMENT_FAILURE = re.compile(
    r"(?i)command not found|executable `[^`]*` not found|`?pre-commit`? not found"
    r"|\bnot found\b|no such file or directory|permission denied"
    r"|an unexpected error has occurred|traceback \(most recent call last\)"
    r"|modulenotfounderror|importerror|filenotfounderror|not installed"
    r"|invalidconfigerror|\benoent\b|exec format error"
)
_NON_REPAIRABLE_EXIT_CODES = frozenset({126, 127})
RepairOutcome = Literal["not_attempted", "turn_failed", "rejected"]
_REPAIR_LINES: dict[RepairOutcome, str] = {
    "not_attempted": "Repair turn: not attempted.",
    "turn_failed": "Repair turn: attempted once; the fix agent's repair turn failed.",
    "rejected": "Repair turn: attempted once; the commit was rejected again.",
}


class CommitHookFailedError(subprocess.CalledProcessError):
    """A ``git commit`` failure while a commit hook is installed.

    A ``CalledProcessError`` subclass, so every existing ``except`` clause
    still catches it. ``hook`` names the installed hook script.
    """

    def __init__(
        self,
        returncode: int,
        cmd: list[str],
        *,
        output: str,
        stderr: str,
        hook: str,
    ) -> None:
        super().__init__(returncode, cmd, output=output, stderr=stderr)
        self.hook = hook


def installed_hook(worktree: Path) -> str | None:
    """Return the first executable ``pre-commit``/``commit-msg`` hook's name.

    Resolves the hooks directory through :func:`cw._git.git_hooks_dir`, which
    joins a relative ``core.hooksPath`` onto the worktree root.
    """
    hooks_dir = git_hooks_dir(worktree)
    for name in _HOOK_NAMES:
        script = hooks_dir / name
        if script.is_file() and os.access(script, os.X_OK):
            return name
    return None


def as_hook_failure(
    worktree: Path, exc: subprocess.CalledProcessError, cycle: int
) -> subprocess.CalledProcessError:
    """Log *exc*'s full output, then return it as a hook failure if one applies.

    Returns a :class:`CommitHookFailedError` copy when a commit hook is
    installed, else *exc* itself (a plain git failure stays ``codex_error``).
    """
    _log.warning(
        "codex fix cycle %d: git commit rejected by a commit hook (exit code %d, "
        "command %s)\n--- stdout ---\n%s\n--- stderr ---\n%s",
        cycle,
        exc.returncode,
        " ".join(map(str, exc.cmd)),
        exc.stdout or "",
        exc.stderr or "",
    )
    hook = installed_hook(worktree)
    if hook is None:
        return exc
    return CommitHookFailedError(
        exc.returncode,
        list(map(str, exc.cmd)),
        output=exc.stdout or "",
        stderr=exc.stderr or "",
        hook=hook,
    )


def _combined(exc: subprocess.CalledProcessError) -> str:
    return "\n".join(part for part in (exc.stdout, exc.stderr) if part)


def failed_hooks(exc: CommitHookFailedError) -> list[tuple[str, int]]:
    """Return ``(hook id, exit code)`` per failed pre-commit-framework hook.

    Falls back to the installed hook script's name and the commit's own exit
    code when the output carries no ``- hook id:`` lines.
    """
    output = _combined(exc)
    ids = _HOOK_ID.findall(output)
    codes = [int(code) for code in _EXIT_CODE.findall(output)]
    if not ids:
        return [(exc.hook, exc.returncode)]
    return [
        (hook_id, codes[i] if i < len(codes) else exc.returncode)
        for i, hook_id in enumerate(ids)
    ]


def summarize_hook_output(output: str) -> str:
    """The postable excerpt: secret lines withheld, redacted, 20 lines/1,000 chars."""
    return redact_and_cap(
        withhold_secret_lines(output),
        max_line_chars=_HOOK_EXCERPT_MAX_CHARS,
        max_lines=_HOOK_EXCERPT_MAX_LINES,
        max_total_chars=_HOOK_EXCERPT_MAX_CHARS,
    )


def _is_secret_scanner(output: str, hook_names: str) -> bool:
    """Whether the hook output or the hook's name points at a secret scanner."""
    return bool(_SECRET_SCANNER.search(output) or _SECRET_SCANNER.search(hook_names))


def repair_excerpt(exc: CommitHookFailedError, measured: Iterable[str]) -> str | None:
    """Return the hook excerpt a repair turn may see, or ``None`` to park.

    Fail-closed (#2987): a repair turn runs only for a content failure, which
    is every check below failing to match. Refused: a ``commit-msg`` hook
    (cw's message, not the agent's content), a secret scanner (its output may
    echo a secret, which must never reach an LLM), exit codes 126/127 from a
    pre-commit-framework hook, environment markers (missing binary, broken
    install), and an excerpt that shows no measured path to repair.
    """
    if exc.hook == "commit-msg":
        return None
    output = _combined(exc)
    hooks = failed_hooks(exc)
    hook_names = ", ".join(name for name, _ in hooks)
    if _is_secret_scanner(output, hook_names):
        return None
    if any(code in _NON_REPAIRABLE_EXIT_CODES for _, code in hooks):
        return None
    if _ENVIRONMENT_FAILURE.search(output):
        return None
    excerpt = summarize_hook_output(output)
    if not any(_mentions_path(excerpt, path) for path in measured):
        return None
    return excerpt


def _mentions_path(text: str, path: str) -> bool:
    """Whether *path* appears in *text* as a whole path, not a suffix of one."""
    return re.search(rf"(?<![\w./-]){re.escape(path)}(?!\w)", text) is not None


def hook_failure_breach(
    exc: CommitHookFailedError,
    cycle: int,
    *,
    repair: RepairOutcome = "not_attempted",
) -> FenceBreach:
    """Return the ``codex_fix_hook_failed`` park for a rejected fix commit.

    *repair* names what the one repair turn did (#2987); it appends exactly one
    line, and the existing details and hint text stay verbatim.
    """
    output = _combined(exc)
    hooks = failed_hooks(exc)
    hook_names = ", ".join(h for h, _ in hooks)
    hook_id = redact_and_cap(hook_names, max_total_chars=_HOOK_ID_MAX_CHARS)
    rc = hooks[0][1]
    if _is_secret_scanner(output, hook_names):
        details = (
            f"codex fix cycle {cycle} produced changes, but `git commit` was "
            f"rejected by a secret-scanning hook (hook: {hook_id}, exit code {rc}). "
            "The hook's output is deliberately not posted here because it may "
            "echo the matched secret. Nothing was committed or pushed.\n\n"
            f"{_LOG_POINTER}\n{_REPAIR_LINES[repair]}"
        )
    else:
        details = (
            f"codex fix cycle {cycle} produced changes, but `git commit` was "
            f"rejected by a commit hook (hook: {hook_id}, exit code {rc}) after "
            "one re-stage retry. Nothing was committed or pushed.\n\n"
            "First lines of hook output (redacted, at most 20 lines / 1000 "
            f"characters):\n{summarize_hook_output(output)}\n\n{_LOG_POINTER}\n"
            f"{_REPAIR_LINES[repair]}"
        )
    hint = (
        "Run the repository's commit hooks on the change (`git commit`, or "
        "`pre-commit run` for pre-commit-managed hooks) to see the full output, "
        f"fix what they report, then requeue REVIEW. {LEFT_STAGED_HINT}"
    )
    return FenceBreach(CODEX_FIX_HOOK_FAILED, (), details, hint)
