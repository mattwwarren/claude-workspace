"""Per-client commit-hook check for ``cw doctor`` (#2973).

A repo whose ``.pre-commit-config.yaml`` declares commit-stage hooks expects
``git commit`` to run them. Three silent states make it run nothing, in this
repo or in every worktree dispatch cuts from it:

* no executable ``pre-commit`` hook at the path git resolves (never installed,
  or a ``core.hooksPath`` set where ``pre-commit install`` refuses to run);
* the hook file exists but is not executable, so git skips it;
* the hook records an ``INSTALL_PYTHON`` that no longer exists (typically
  installed from a deleted worktree's venv), so it fails with "`pre-commit`
  not found" unless a ``pre-commit`` is on ``PATH``.

Read-only: nothing here installs a hook or sets git config. Every finding is
``ok=True, warn=True`` (advisory), so it never fails a doctor run; a healthy
hook is reported ``ok=True, warn=False``. A repo that declares no commit-stage
hook yields no result at all. :func:`commit_hook_state` is also the probe
``cw dev-queue status`` bounds with a short timeout (see ``transient``).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple

import yaml

from cw._git import git_hooks_dir, run_git
from cw.doctor._shared import CheckResult
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from cw.models import ClientConfig

_CONFIG_NAME: Final = ".pre-commit-config.yaml"
_HOOK_NAME: Final = "pre-commit"
_COMMIT_STAGE: Final = "pre-commit"
_DEFAULT_TIMEOUT: Final = 10.0
_INSTALL_CMD: Final = "pre-commit install --hook-type pre-commit"

#: pre-commit's legacy stage names, normalised as ``clientlib.py`` does.
_LEGACY_STAGES: Final = {
    "commit": "pre-commit",
    "merge-commit": "pre-merge-commit",
    "push": "pre-push",
}

#: Every stage a hook can run in; a hook with no ``stages`` and a config with
#: no ``default_stages`` runs in all of them.
_ALL_STAGES: Final = frozenset(
    {
        "commit-msg",
        "manual",
        "post-checkout",
        "post-commit",
        "post-merge",
        "post-rewrite",
        "pre-commit",
        "pre-merge-commit",
        "pre-push",
        "pre-rebase",
        "prepare-commit-msg",
    }
)

_INSTALL_PYTHON_RE: Final = re.compile(r"^INSTALL_PYTHON=(.*)$", re.MULTILINE)


class CommitHookState(NamedTuple):
    """One client's commit-hook verdict.

    ``transient`` is set only on the could-not-resolve state (git failed or
    timed out): ``cw doctor`` reports it, ``cw dev-queue status`` hides it.
    """

    ok: bool
    detail: str
    transient: bool = False


def _normalize_stages(stages: list[object]) -> set[str]:
    """Legacy names mapped to current ones; non-string entries ignored."""
    return {_LEGACY_STAGES.get(s, s) for s in stages if isinstance(s, str)}


def _effective_stages(hook: dict[str, object], default_stages: object) -> set[str]:
    """The stages *hook* runs in: its own, else the config default, else all.

    An empty ``stages`` list inherits ``default_stages``; an explicit empty
    ``default_stages`` stays empty.
    """
    own = hook.get("stages")
    if isinstance(own, list) and own:
        return _normalize_stages(own)
    if isinstance(default_stages, list):
        return _normalize_stages(default_stages)
    return set(_ALL_STAGES)


def declares_commit_stage_hook(config_text: str) -> bool:
    """Whether a ``.pre-commit-config.yaml`` declares a hook that runs at commit.

    ``manual``-only and ``pre-push``-only hooks do not count.
    ``default_install_hook_types`` is deliberately ignored: it governs what
    ``pre-commit install`` installs, not what fires. Remote hooks inherit
    ``default_stages`` (or all stages) because their manifests are not read
    offline, so a manifest hook that is pre-push-only upstream may be
    over-reported. Malformed or non-mapping input is ``False``.
    """
    try:
        config = yaml.safe_load(config_text)
    except yaml.YAMLError:
        return False
    if not isinstance(config, dict):
        return False
    repos = config.get("repos")
    if not isinstance(repos, list):
        return False
    default_stages = config.get("default_stages")
    for repo in repos:
        hooks = repo.get("hooks") if isinstance(repo, dict) else None
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if isinstance(hook, dict) and _COMMIT_STAGE in _effective_stages(
                hook, default_stages
            ):
                return True
    return False


def _read_text(path: Path) -> str | None:
    """*path*'s text (undecodable bytes replaced), or ``None`` if unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _hooks_path_setting(repo: Path, timeout: float) -> tuple[str, str] | None:
    """``(value, origin)`` of ``core.hooksPath`` in *repo*, or ``None`` if unset."""
    try:
        done = run_git(
            ["config", "--show-origin", "--get", "core.hooksPath"],
            cwd=repo,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    origin, _, value = done.stdout.strip().partition("\t")
    return value, origin


def _install_python(script_text: str) -> str | None:
    """The absolute interpreter a pre-commit-generated hook records, if any.

    ``None`` for a hook with no ``INSTALL_PYTHON`` line (husky, hand-written),
    an empty or unparseable value, or a relative one (resolved by the
    caller's ``PATH`` at run time, not checkable here).
    """
    match = _INSTALL_PYTHON_RE.search(script_text)
    if match is None:
        return None
    try:
        parts = shlex.split(match.group(1))
    except ValueError:
        return None
    return parts[0] if parts and Path(parts[0]).is_absolute() else None


def _hooks_path_note(setting: tuple[str, str] | None) -> str:
    if setting is None:
        return ""
    value, origin = setting
    return f" (core.hooksPath is set to '{value}' ({origin}))"


def _missing_detail(repo: Path, hooks: Path, setting: tuple[str, str] | None) -> str:
    if setting is not None:
        value, origin = setting
        return (
            f"core.hooksPath is set to '{value}' ({origin}), so hooks resolve to "
            f"{hooks}, which has no executable {_HOOK_NAME} hook, and "
            "`pre-commit install` refuses to install while core.hooksPath is set. "
            f"Fix: run `git -C {repo} config --unset core.hooksPath`, then "
            f"`{_INSTALL_CMD}`; or, if the setting comes from your global config, "
            "unset it there."
        )
    return (
        f"no executable {_HOOK_NAME} hook at {hooks / _HOOK_NAME}: "
        f"{_CONFIG_NAME} declares commit-stage hooks, but git commit runs no "
        "checks in this repo or any of its worktrees. "
        f"Fix: run `{_INSTALL_CMD}` in {repo}"
    )


def _not_executable_detail(script: Path, setting: tuple[str, str] | None) -> str:
    return (
        f"{script} exists but is not executable, so git skips it. "
        f"Fix: chmod +x {script}{_hooks_path_note(setting)}"
    )


def _stale_detail(repo: Path, script: Path, python: str) -> str:
    return (
        f"the {_HOOK_NAME} hook at {script} records INSTALL_PYTHON={python}, "
        "which is not an executable file (typically a hook installed from a "
        'deleted worktree\'s venv); commits fail with "`pre-commit` not found" '
        f"unless pre-commit is on PATH. Fix: re-run `{_INSTALL_CMD}` from a "
        f"live environment in {repo}"
    )


def commit_hook_state(
    repo: Path, *, timeout: float = _DEFAULT_TIMEOUT
) -> CommitHookState | None:
    """Judge *repo*'s commit hook, or ``None`` when the repo declares none.

    *timeout* bounds every git call. Limitation: a relative ``core.hooksPath``
    resolves from each worktree's own root; this resolves it against *repo*
    (the shared clone) only and does not verify linked worktrees.
    """
    config_text = _read_text(repo / _CONFIG_NAME)
    if config_text is None or not declares_commit_stage_hook(config_text):
        return None
    try:
        hooks = git_hooks_dir(repo, timeout=timeout)
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
        return CommitHookState(
            ok=False,
            detail=(
                f"could not resolve the git hooks directory in {repo} "
                f"({type(exc).__name__}); is it a git checkout?"
            ),
            transient=True,
        )
    script = hooks / _HOOK_NAME
    if not script.is_file():
        setting = _hooks_path_setting(repo, timeout)
        return CommitHookState(ok=False, detail=_missing_detail(repo, hooks, setting))
    if not os.access(script, os.X_OK):
        setting = _hooks_path_setting(repo, timeout)
        return CommitHookState(ok=False, detail=_not_executable_detail(script, setting))
    python = _install_python(_read_text(script) or "")
    if python is not None and not _is_executable_file(Path(python)):
        return CommitHookState(ok=False, detail=_stale_detail(repo, script, python))
    return CommitHookState(ok=True, detail=f"{_HOOK_NAME} hook installed at {script}")


def _check_commit_hook(clients: dict[str, ClientConfig]) -> list[CheckResult]:
    """One ``commit-hook/<client>`` result per client that declares commit hooks."""
    results: list[CheckResult] = []
    for name, client in clients.items():
        state = commit_hook_state(_git_dir(client))
        if state is None:
            continue
        results.append(
            CheckResult(
                f"commit-hook/{name}",
                ok=True,
                warn=not state.ok,
                detail=state.detail,
            )
        )
    return results
