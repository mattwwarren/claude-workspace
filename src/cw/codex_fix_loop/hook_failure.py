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
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from cw._git import git_output
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT, FenceBreach
from cw.codex_fix_loop.posted_text import redact_and_cap, withhold_secret_lines
from cw.codex_review import CODEX_FIX_HOOK_FAILED

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

    Resolves ``git rev-parse --git-path hooks`` against the worktree root,
    since git prints a relative ``core.hooksPath`` as is.
    """
    hooks = Path(git_output(["rev-parse", "--git-path", "hooks"], cwd=worktree).strip())
    hooks_dir = hooks if hooks.is_absolute() else worktree / hooks
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


def hook_failure_breach(exc: CommitHookFailedError, cycle: int) -> FenceBreach:
    """Return the ``codex_fix_hook_failed`` park for a rejected fix commit."""
    output = _combined(exc)
    hooks = failed_hooks(exc)
    hook_id = redact_and_cap(
        ", ".join(h for h, _ in hooks), max_total_chars=_HOOK_ID_MAX_CHARS
    )
    rc = hooks[0][1]
    if _SECRET_SCANNER.search(output) or _SECRET_SCANNER.search(hook_id):
        details = (
            f"codex fix cycle {cycle} produced changes, but `git commit` was "
            f"rejected by a secret-scanning hook (hook: {hook_id}, exit code {rc}). "
            "The hook's output is deliberately not posted here because it may "
            "echo the matched secret. Nothing was committed or pushed.\n\n"
            f"{_LOG_POINTER}"
        )
    else:
        details = (
            f"codex fix cycle {cycle} produced changes, but `git commit` was "
            f"rejected by a commit hook (hook: {hook_id}, exit code {rc}) after "
            "one re-stage retry. Nothing was committed or pushed.\n\n"
            "First lines of hook output (redacted, at most 20 lines / 1000 "
            f"characters):\n{summarize_hook_output(output)}\n\n{_LOG_POINTER}"
        )
    hint = (
        "Run the repository's commit hooks in the worktree (`git commit`, or "
        "`pre-commit run` for pre-commit-managed hooks) to see the full output, "
        f"fix what they report, then requeue REVIEW. {LEFT_STAGED_HINT}"
    )
    return FenceBreach(CODEX_FIX_HOOK_FAILED, (), details, hint)
