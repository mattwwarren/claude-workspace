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
delete. Advisory severity (``ok=True, warn=True``), so it never fails
``cw doctor``'s exit code. Leaf module — no cross-``doctor`` dependencies.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from cw.doctor._shared import CheckResult

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


def _stop_hook_locations(data: object) -> list[tuple[str, str]]:
    """Return ``(location, command)`` pairs for every cw Stop hook in *data*.

    Pure function over already-parsed JSON. Only the ``Stop`` event is
    inspected; ``PreToolUse`` and ``SessionStart`` cw hooks are legitimate
    user-level configuration.
    """
    if not isinstance(data, dict):
        return []
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


def _scan_settings_file(path: Path) -> tuple[list[str], bool]:
    """Scan one settings file for cw Stop hooks.

    Returns ``(findings, skipped)``. A missing file is the ordinary case and
    yields ``([], False)``. Anything we cannot read or parse — an unreadable
    path, a directory where a file was expected, malformed JSON — yields
    ``([], True)`` so the caller can say so without failing the check;
    ``bypass-disclaimer`` already warns on a malformed ``settings.json``.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ([], False)
    except OSError:
        return ([], True)

    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError:
        return ([], True)

    return (
        [
            _format_finding(path, location, command)
            for location, command in _stop_hook_locations(data)
        ],
        False,
    )


def _check_user_level_stop_hook() -> CheckResult:
    """Flag a ``cw signal-stop`` Stop hook installed in user-level settings.

    Warns (``ok=True, warn=True``) when either ``~/.claude/settings.json`` or
    ``~/.claude/settings.local.json`` wires the Stop event to cw, naming each
    finding's file, ``hooks.Stop`` coordinates and the exact line to remove.
    Clean (``ok=True, warn=False``) otherwise, including when the files are
    absent or unparseable — an unparseable file is noted in the detail rather
    than warned on. Never ``ok=False``: a user-level hook is a performance
    regression, not a broken environment.
    """
    findings: list[str] = []
    skipped: list[str] = []

    for filename in _USER_SETTINGS_FILENAMES:
        path = _CLAUDE_HOME / filename
        file_findings, was_skipped = _scan_settings_file(path)
        findings.extend(file_findings)
        if was_skipped:
            skipped.append(f"(skipped unparseable {path})")

    if findings:
        return CheckResult(
            _CHECK_NAME, ok=True, warn=True, detail="; ".join(findings + skipped)
        )

    detail = "no cw Stop hook in user-level settings"
    if skipped:
        detail = f"{detail} {' '.join(skipped)}"
    return CheckResult(_CHECK_NAME, ok=True, warn=False, detail=detail)
