"""Plan file parsing for workspace plan visibility."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class PlanTask:
    text: str
    completed: bool
    phase: str


@dataclass
class PlanPhase:
    name: str
    tasks: list[PlanTask] = field(default_factory=list)

    @property
    def progress(self) -> tuple[int, int]:
        """Return (completed, total) task counts."""
        total = len(self.tasks)
        done = sum(1 for t in self.tasks if t.completed)
        return done, total


@dataclass
class PlanSummary:
    path: Path
    title: str
    phases: list[PlanPhase] = field(default_factory=list)

    @property
    def progress(self) -> tuple[int, int]:
        """Return aggregate (completed, total) across all phases."""
        done = sum(p.progress[0] for p in self.phases)
        total = sum(p.progress[1] for p in self.phases)
        return done, total


_CHECKBOX_RE = re.compile(r"^-\s+\[([ xX])\]\s+(.+)$")
_H1_RE = re.compile(r"^#\s+(.+)$")
_H2_RE = re.compile(r"^##\s+(.+)$")


def find_plan_files(workspace_path: Path) -> list[Path]:
    """Find plan files in .claude/plans/ directory.

    Returns paths sorted by mtime (newest first).
    """
    plans_dir = workspace_path / ".claude" / "plans"
    if not plans_dir.is_dir():
        return []

    candidates: list[Path] = []
    # Direct .md files
    candidates.extend(plans_dir.glob("*.md"))
    # Nested directories
    candidates.extend(plans_dir.glob("*/*.md"))

    if not candidates:
        return []

    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def parse_plan(plan_path: Path) -> PlanSummary:
    """Parse a markdown plan file.

    Extracts H1 as title, H2s as phases, and checkbox items as tasks.
    """
    content = plan_path.read_text()
    lines = content.splitlines()

    title = plan_path.stem
    phases: list[PlanPhase] = []
    current_phase: PlanPhase | None = None

    for line in lines:
        h1_match = _H1_RE.match(line)
        if h1_match:
            title = h1_match.group(1).strip()
            continue

        h2_match = _H2_RE.match(line)
        if h2_match:
            current_phase = PlanPhase(name=h2_match.group(1).strip())
            phases.append(current_phase)
            continue

        cb_match = _CHECKBOX_RE.match(line)
        if cb_match and current_phase is not None:
            completed = cb_match.group(1) in ("x", "X")
            text = cb_match.group(2).strip()
            current_phase.tasks.append(
                PlanTask(text=text, completed=completed, phase=current_phase.name),
            )

    return PlanSummary(path=plan_path, title=title, phases=phases)
