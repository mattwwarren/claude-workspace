"""User-level Stop-hook scope check for cw doctor (#2226).

cw's completion signal is injected *per worktree*: ``spawn._write_hook_context``
writes ``<worktree>/.claude/settings.local.json`` alongside the
``cw-context.json`` the hook reads (ADR-0003). A copy of that Stop hook in
``~/.claude/settings.json`` or ``~/.claude/settings.local.json`` applies to
**every** Claude session on the machine, cw-managed or not, and each turn of
each of those sessions then pays a Python interpreter start only to discover
there is no context file to act on.

No cw install path writes such a hook — ``scripts/install-skills.sh`` touches
only ``~/.claude/{commands,skills,agents,scripts}`` and ``cw init`` writes only
the ``Bash(cw:*)`` allowlist entry — so the root cause of an observed
user-level install is unidentified. This check is therefore detection only: it
names the offending file, the exact ``hooks.Stop`` coordinates, and the line to
delete. A settings file it cannot read or parse is itself a WARN finding, not a
crash. Advisory severity (``ok=True, warn=True``), so it never fails
``cw doctor``'s exit code. Leaf module — no cross-``doctor`` dependencies.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from cw.doctor._shared import CheckResult, SettingsReadFailure, _read_settings

# Home-tree root scanned for a user-level Stop hook — a module-level
# Path.home()-derived constant, patched in tests via
# monkeypatch.setattr("cw.doctor.user_level_hooks._CLAUDE_HOME", ...).
_CLAUDE_HOME = Path.home() / ".claude"

# Check name for the user-level Stop-hook scope detector.
_CHECK_NAME = "stop-hook-scope"

# User-level settings files Claude Code reads. settings.local.json is included
# because a cw session spawned with cwd=$HOME would make _write_hook_context
# write that very file.
_USER_SETTINGS_FILENAMES = ("settings.json", "settings.local.json")

# Matches every spelling of the injected command: the legacy bare
# ``cw signal-stop``, the #2226 shell-guarded form, and an absolute-path
# invocation such as ``/opt/bin/cw signal-stop``. Deliberately narrow — sibling
# user-level cw hooks (``cw guard-cwd``, ``cw orchestrate status``) are
# legitimate config and must not match.
_STOP_HOOK_PATTERN = re.compile(r"\bcw\s+signal-stop\b")

# Appended to every finding: what the operator should understand about why a
# user-level copy is wrong, not just that it was found.
_REMEDIATION_SUFFIX = (
    "cw injects this hook per-worktree via "
    "<worktree>/.claude/settings.local.json; a user-level copy costs ~280ms "
    "of interpreter startup on every turn of every Claude session."
)


def _hook_locations_in_entry(entry_index: int, entry: object) -> list[tuple[str, str]]:
    """Return ``(location, command)`` pairs for one ``hooks.Stop`` entry.

    Every level is ``isinstance``-narrowed: hand-edited settings files carry
    arbitrary JSON, and a wrong shape must degrade to "nothing found" rather
    than raise out of ``run_doctor``.
    """
    if not isinstance(entry, dict):
        return []
    entry_hooks: object = entry.get("hooks")
    if not isinstance(entry_hooks, list):
        return []

    found: list[tuple[str, str]] = []
    for hook_index, hook in enumerate(entry_hooks):
        if not isinstance(hook, dict):
            continue
        command: object = hook.get("command")
        if isinstance(command, str) and _STOP_HOOK_PATTERN.search(command):
            found.append((f"hooks.Stop[{entry_index}].hooks[{hook_index}]", command))
    return found


def _stop_hook_locations(data: dict[str, object]) -> list[tuple[str, str]]:
    """Return ``(location, command)`` pairs for every cw Stop hook in *data*.

    Pure function over an already-parsed settings object (the shared reader
    guarantees the top level is a dict). Only the ``Stop`` event is
    inspected; ``PreToolUse`` and ``SessionStart`` cw hooks are legitimate
    user-level configuration.
    """
    hooks: object = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    stop_entries: object = hooks.get("Stop")
    if not isinstance(stop_entries, list):
        return []

    found: list[tuple[str, str]] = []
    for entry_index, entry in enumerate(stop_entries):
        found.extend(_hook_locations_in_entry(entry_index, entry))
    return found


def _format_finding(path: Path, location: str, command: str) -> str:
    """One human-actionable line naming the file, the coordinates and the fix."""
    return (
        f'{path}: {location} runs "cw signal-stop" — delete the line '
        f"'\"command\": {json.dumps(command)}' and its enclosing "
        '{"type": "command"} object (and the Stop entry if it becomes '
        f"empty). {_REMEDIATION_SUFFIX}"
    )


def _scan_settings_file(path: Path) -> tuple[list[str], str | None]:
    """Scan one settings file for cw Stop hooks.

    Returns ``(findings, problem)``. A missing file is the ordinary case and
    yields ``([], None)``. A file we cannot read or parse yields
    ``([], <failure class>)`` so the caller can WARN naming the file — a check
    whose whole purpose is to diagnose a broken install must not crash on a
    malformed one, and must not silently pass it either. The read goes through
    the shared :func:`cw.doctor._shared._read_settings`, whose failure class is
    a short label (``invalid UTF-8``, ``malformed JSON``, ``not a JSON
    object``, ``unreadable: <ExcName>``), never exception text, which can echo
    file contents. Valid JSON *object* of the wrong shape below the top level
    is not a read/parse failure: it simply has no Stop hook to find.
    """
    data = _read_settings(path)
    if isinstance(data, SettingsReadFailure):
        return ([], None if data.missing else data.reason)

    return (
        [
            _format_finding(path, location, command)
            for location, command in _stop_hook_locations(data)
        ],
        None,
    )


def _check_user_level_stop_hook() -> CheckResult:
    """Flag a ``cw signal-stop`` Stop hook installed in user-level settings.

    Warns (``ok=True, warn=True``) when either ``~/.claude/settings.json`` or
    ``~/.claude/settings.local.json`` wires the Stop event to cw, naming each
    finding's file, ``hooks.Stop`` coordinates and the exact line to remove. It
    also warns, naming the file and the failure class, when a settings file
    exists but cannot be read or parsed (invalid UTF-8, malformed JSON, an
    unreadable path): the other file is still scanned and the check never
    raises out of ``run_doctor``. Clean (``ok=True, warn=False``) when both
    files are absent or hold no cw Stop hook. Never ``ok=False``: a user-level
    hook is a performance regression, not a broken environment.
    """
    warnings: list[str] = []

    for filename in _USER_SETTINGS_FILENAMES:
        path = _CLAUDE_HOME / filename
        findings, problem = _scan_settings_file(path)
        warnings.extend(findings)
        if problem is not None:
            warnings.append(f"{path}: could not read/parse ({problem})")

    if warnings:
        return CheckResult(_CHECK_NAME, ok=True, warn=True, detail="; ".join(warnings))
    return CheckResult(
        _CHECK_NAME,
        ok=True,
        warn=False,
        detail="no cw Stop hook in user-level settings",
    )
