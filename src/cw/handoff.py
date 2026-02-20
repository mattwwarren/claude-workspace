"""Handoff document parsing for session resume."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.models import HandoffReason, TaskSpec

if TYPE_CHECKING:
    from pathlib import Path

HANDOFF_GLOB = "session-*.md"
_log = logging.getLogger(__name__)


def find_latest_handoff(workspace_path: Path) -> Path | None:
    """Find the most recent session handoff in a workspace's .handoffs/ directory.

    Checks both workspace-level and ~/.claude/plans/ for handoff files.
    """
    candidates: list[Path] = []

    # Workspace-level handoffs
    handoffs_dir = workspace_path / ".handoffs"
    if handoffs_dir.is_dir():
        candidates.extend(handoffs_dir.glob(HANDOFF_GLOB))

    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def find_handoffs_newer_than(workspace_path: Path, since_mtime: float) -> list[Path]:
    """Find handoff files newer than a given mtime.

    Used by background_session to detect newly generated handoffs.
    """
    handoffs_dir = workspace_path / ".handoffs"
    if not handoffs_dir.is_dir():
        return []

    # Cache stat results to avoid double stat() per file
    timed = [
        (p, p.stat().st_mtime)
        for p in handoffs_dir.glob(HANDOFF_GLOB)
    ]
    return [
        p for p, mtime in sorted(timed, key=lambda t: t[1], reverse=True)
        if mtime > since_mtime
    ]


def build_cross_session_prompt(
    source_purpose: str,
    target_purpose: str,
    branch: str | None,
    raw_prompt: str | None,
) -> str:
    """Wrap a resumption prompt with cross-session context."""
    branch_label = f" on branch {branch}" if branch else ""
    header = (
        f"Cross-session handoff: {source_purpose} → {target_purpose}"
        f"{branch_label}."
    )

    if raw_prompt:
        return (
            f"{header}\n"
            f"The {source_purpose} session completed with this context:\n\n"
            f"{raw_prompt}"
        )

    return (
        f"{header}\n"
        f"The {source_purpose} session has been backgrounded."
        f" No resumption context was captured."
    )


def extract_resumption_prompt(handoff_path: Path) -> str | None:
    """Extract the resumption prompt from a handoff document.

    Looks for the ## Resumption Prompt section and extracts the content
    from the code block within it.
    """
    try:
        content = handoff_path.read_text()
    except OSError:
        return None

    # Find the Resumption Prompt section
    section_pattern = re.compile(
        r"^## Resumption Prompt\s*\n"
        r".*?"  # Optional text between heading and code block
        r"```\s*\n"
        r"(.*?)"
        r"```",
        re.MULTILINE | re.DOTALL,
    )

    match = section_pattern.search(content)
    if match:
        return match.group(1).strip()

    return None


def write_structured_handoff(
    workspace_path: Path,
    source_session: str,
    tasks: list[TaskSpec],
    *,
    branch: str | None = None,
    recent_changes: list[str] | None = None,
    blockers: list[str] | None = None,
) -> Path:
    """Write a JSON sidecar alongside the markdown handoff for machine parsing."""
    handoffs_dir = workspace_path / ".handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    ts = now.strftime("%Y%m%dT%H%M%S")
    sidecar_path = handoffs_dir / f"session-structured-{ts}.json"

    payload = {
        "version": 1,
        "source_session": source_session,
        "timestamp": now.isoformat(),
        "tasks": [t.model_dump(mode="json") for t in tasks],
        "context": {
            "branch": branch,
            "recent_changes": recent_changes or [],
            "blockers": blockers or [],
        },
    }
    sidecar_path.write_text(json.dumps(payload, indent=2))
    return sidecar_path


def parse_structured_handoff(path: Path) -> list[TaskSpec]:
    """Read a JSON sidecar and return a list of TaskSpec objects.

    Falls back gracefully if the file doesn't exist or is malformed.
    """
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    tasks_raw = raw.get("tasks", [])
    results: list[TaskSpec] = []
    for t in tasks_raw:
        try:
            results.append(TaskSpec.model_validate(t))
        except Exception:
            _log.debug("Skipping malformed task: %s", t)
            continue
    return results


def build_task_prompt(task: TaskSpec) -> str:
    """Convert a TaskSpec into a well-formatted prompt for Claude."""
    lines = [f"Task: {task.description}", ""]
    if task.context_files:
        lines.append("Context files:")
        lines.extend(f"  - {f}" for f in task.context_files)
        lines.append("")
    if task.success_criteria:
        lines.append(f"Success criteria: {task.success_criteria}")
        lines.append("")
    lines.append("Instructions:")
    lines.append(task.prompt)
    return "\n".join(lines)


def parse_handoff_reason(handoff_path: Path) -> HandoffReason | None:
    """Extract the reason field from handoff frontmatter, if present.

    Returns a :class:`HandoffReason` member or ``None`` (normal completion).
    Normal /session-done handoffs lack frontmatter with a reason field.
    Abnormal /handoff handoffs include ``reason:`` in YAML frontmatter.
    Unrecognised reason values are ignored (returns ``None``).
    """
    try:
        content = handoff_path.read_text()
    except OSError:
        return None

    # Check for YAML frontmatter delimited by ---
    if not content.startswith("---"):
        return None

    end = content.find("---", 3)
    if end == -1:
        return None

    frontmatter = content[3:end]
    match = re.search(r"^reason:\s*(.+)$", frontmatter, re.MULTILINE)
    if not match:
        return None

    raw = match.group(1).strip()
    try:
        return HandoffReason(raw)
    except ValueError:
        _log.warning("Unknown handoff reason %r, treating as normal", raw)
        return None


_DAEMON_WORKFLOW_TEMPLATE = (
    "You have been assigned a task by the daemon queue system."
    " Complete it autonomously.\n"
    "\n"
    "## Task\n"
    "\n"
    "{task_prompt}\n"
    "\n"
    "## Workflow\n"
    "\n"
    "1. **Assess**: Read relevant code. If the task is complex"
    " (multi-file, architectural),"
    " plan your approach before implementing.\n"
    "2. **Implement**: Write the code changes.\n"
    "3. **Quality gates**: Run `ruff check src/ tests/`,"
    " `mypy src/`, and `pytest tests/ -v`."
    " Fix any issues before proceeding.\n"
    "4. **Review**: Spawn review agents using the Task tool"
    " to review your changes."
    " Use Code Quality Reviewer and Architecture Reviewer"
    " at minimum. Fix all HIGH and MEDIUM findings."
    " Do NOT review inline — always delegate to agents"
    " to protect your context window.\n"
    "5. **Commit**: Create a git commit with a clear message.\n"
    "6. **Signal completion**: Run /session-done to generate"
    " a handoff and signal you are finished.\n"
    "\n"
    "## Important\n"
    "\n"
    "- If you run out of context or hit a blocker you cannot"
    " resolve after 2 attempts,"
    " use `/handoff --reason context` instead of"
    " `/session-done`. The daemon will detect this"
    " and pause for human intervention.\n"
    "- Stay focused on this single task."
    " Do not expand scope.\n"
    "- Keep your context lean: delegate reviews to agents,"
    " don't read unnecessary files.\n"
)


def build_daemon_workflow_prompt(task: TaskSpec) -> str:
    """Wrap a TaskSpec in autonomous workflow instructions for daemon execution."""
    task_prompt = build_task_prompt(task)
    return _DAEMON_WORKFLOW_TEMPLATE.format(task_prompt=task_prompt)
