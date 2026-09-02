"""Guard tests for `/prep-pr` Step 8's project ship-it detection.

Step 8 used to probe a single path (`test -f .claude/commands/ship-it.md`).
A repo whose ship-it ships as a *skill* — `.claude/skills/ship-it/SKILL.md`,
often a symlink into `.agents/skills/ship-it/` — therefore read as "no project
ship-it", so Step 8 took the STOP branch (headless: a `no project /ship-it`
BLOCK) for a repo that could ship perfectly well.

Group A pins the three probed layouts in the prose. Group B follows
`test_ship_it_title_tiers.py` and *executes* the probe fence against real
temp trees: prose can confirm the paths were written down, but only running
the loop falsifies "does this actually find a skill-layout ship-it".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PREP_PR_PATH = ROOT / ".claude" / "commands" / "prep-pr.md"
FENCE = "```bash"

CANDIDATE_PATHS = (
    ".claude/commands/ship-it.md",
    ".claude/skills/ship-it/SKILL.md",
    ".agents/skills/ship-it/SKILL.md",
)


def _step_8_probe() -> str:
    """Extract the bash fence from Step 8's ship-it detection block."""
    text = PREP_PR_PATH.read_text(encoding="utf-8")
    anchor = text.index("Check for a project-level ship-it")
    start = text.index(FENCE, anchor) + len(FENCE)
    end = text.index("```", start)
    return text[start:end]


# --- Group A: the probed layouts are written down ---


@pytest.mark.parametrize("candidate", CANDIDATE_PATHS)
def test_step_8_probe_covers_layout(candidate: str) -> None:
    assert candidate in _step_8_probe(), (
        f"Step 8's ship-it probe no longer covers {candidate}; a repo using "
        "that layout will be reported as having no ship-it."
    )


def test_stop_message_lists_every_probed_layout() -> None:
    text = PREP_PR_PATH.read_text(encoding="utf-8")
    stop = text[text.index("This project has no ship-it") :]
    stop = stop[: stop.index("\n\n")]
    for candidate in CANDIDATE_PATHS:
        assert candidate in stop, (
            f"the no-ship-it STOP message omits {candidate}, so the user is "
            "told to create a ship-it without being told where it was looked for"
        )


# --- Group B: the probe actually finds each layout ---


def _run_probe(cwd: Path) -> str:
    result = subprocess.run(
        ["bash", "-c", _step_8_probe()],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def test_probe_finds_command_layout(tmp_path: Path) -> None:
    target = tmp_path / ".claude" / "commands" / "ship-it.md"
    target.parent.mkdir(parents=True)
    target.write_text("# ship it\n", encoding="utf-8")

    assert ".claude/commands/ship-it.md" in _run_probe(tmp_path)


def test_probe_finds_claude_skill_layout(tmp_path: Path) -> None:
    target = tmp_path / ".claude" / "skills" / "ship-it" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("# ship it\n", encoding="utf-8")

    assert ".claude/skills/ship-it/SKILL.md" in _run_probe(tmp_path)


def test_probe_finds_agents_skill_via_symlink(tmp_path: Path) -> None:
    """The layout that produced the bug: `.claude/skills` symlinked into `.agents`."""
    real = tmp_path / ".agents" / "skills" / "ship-it"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("# ship it\n", encoding="utf-8")
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "ship-it").symlink_to(real, target_is_directory=True)

    stdout = _run_probe(tmp_path)
    assert ".claude/skills/ship-it/SKILL.md" in stdout
    assert ".agents/skills/ship-it/SKILL.md" in stdout
    # Both hits resolve to one physical file, so the operator can tell it is
    # one ship-it rather than two competing ones.
    resolved = {line.split(" -> ")[1] for line in stdout.strip().splitlines()}
    assert len(resolved) == 1, stdout


def test_probe_is_silent_when_no_ship_it_exists(tmp_path: Path) -> None:
    assert _run_probe(tmp_path).strip() == ""
