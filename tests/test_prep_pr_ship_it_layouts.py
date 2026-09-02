"""Guard tests for the project ship-it detection shared by `/prep-pr` and `/setup`.

`/prep-pr` Step 8 used to probe a single path (`test -f
.claude/commands/ship-it.md`). A repo whose ship-it ships as a *skill* —
`.claude/skills/ship-it/SKILL.md`, often a symlink into
`.agents/skills/ship-it/` — therefore read as "no project ship-it", so Step 8
took the STOP branch (headless: a `no project /ship-it` BLOCK) for a repo that
could ship perfectly well. `/setup` Step 6 carried the same single-path probe
and would have bootstrapped a command stub beside a working skill, which Step
8's command-wins precedence then prefers over the real thing.

Group A pins every prose copy of the layout list, following
`test_changelog_gate_prose_sync.py` (#1634): that test exists because a
snippet duplicated across files drifts silently when only one copy is
maintained, so it pins *all* copies rather than a representative one. Group B
follows `test_ship_it_title_tiers.py` and *executes* the extracted probe
fences against real temp trees: prose can confirm the paths were written down,
but only running the loop falsifies "does this actually find a skill-layout
ship-it".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PREP_PR_PATH = ROOT / ".claude" / "commands" / "prep-pr.md"
SETUP_PATH = ROOT / ".claude" / "commands" / "setup.md"
FINALIZE_APPENDIX_PATH = ROOT / ".claude" / "commands" / "auto-dev-finalize-appendix.md"
FENCE = "```bash"

CANDIDATE_PATHS = (
    ".claude/commands/ship-it.md",
    ".claude/skills/ship-it/SKILL.md",
    ".agents/skills/ship-it/SKILL.md",
)


def _fence_after(path: Path, anchor: str) -> str:
    """Extract the first bash fence following `anchor` in `path`."""
    text = path.read_text(encoding="utf-8")
    start = text.index(FENCE, text.index(anchor)) + len(FENCE)
    return text[start : text.index("```", start)]


def _paragraph_at(path: Path, anchor: str) -> str:
    """Extract the block of prose starting at `anchor`, up to the next blank line.

    A copy that runs to the end of the file has no trailing blank line, so an
    absent separator means "take the rest of the file" rather than an error.
    """
    text = path.read_text(encoding="utf-8")
    block = text[text.index(anchor) :]
    end = block.find("\n\n")
    return block if end == -1 else block[:end]


def _prep_pr_probe() -> str:
    return _fence_after(PREP_PR_PATH, "Check for a project-level ship-it")


def _setup_probe() -> str:
    return _fence_after(SETUP_PATH, "Check existence")


# Every place the supported-layout list is written down. A layout added to the
# probe but not to these copies leaves a user or operator being pointed at an
# incomplete set of locations.
LAYOUT_LIST_COPIES = {
    "prep-pr probe fence": _prep_pr_probe,
    "prep-pr STOP message": lambda: _paragraph_at(
        PREP_PR_PATH, "This project has no ship-it"
    ),
    "prep-pr Notes bullet": lambda: _paragraph_at(
        PREP_PR_PATH, "- A project-level ship-it is required"
    ),
    "finalize appendix operator prompt": lambda: _paragraph_at(
        FINALIZE_APPENDIX_PATH, "Project has no ship-it in any layout"
    ),
    "setup probe fence": _setup_probe,
    "setup bootstrap prompt": lambda: _paragraph_at(
        SETUP_PATH, "This repo has no ship-it"
    ),
    "setup Step 6 intro": lambda: _paragraph_at(SETUP_PATH, "It may be a command"),
}


# --- Group A: every copy of the layout list stays complete ---


@pytest.mark.parametrize("copy_name", sorted(LAYOUT_LIST_COPIES))
@pytest.mark.parametrize("candidate", CANDIDATE_PATHS)
def test_layout_list_copy_covers_candidate(copy_name: str, candidate: str) -> None:
    assert candidate in LAYOUT_LIST_COPIES[copy_name](), (
        f"{copy_name} no longer names {candidate}; a repo using that layout "
        "will be reported as having no ship-it, or the user will be told to "
        "look in an incomplete set of places."
    )


# --- Group B: the probes actually find each layout ---


def _run(probe: str, cwd: Path) -> str:
    result = subprocess.run(
        ["bash", "-c", probe],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def _write(path: Path, body: str = "# ship it\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_prep_pr_probe_finds_command_layout(tmp_path: Path) -> None:
    _write(tmp_path / ".claude" / "commands" / "ship-it.md")

    assert ".claude/commands/ship-it.md" in _run(_prep_pr_probe(), tmp_path)


def test_prep_pr_probe_finds_claude_skill_layout(tmp_path: Path) -> None:
    _write(tmp_path / ".claude" / "skills" / "ship-it" / "SKILL.md")

    assert ".claude/skills/ship-it/SKILL.md" in _run(_prep_pr_probe(), tmp_path)


def test_prep_pr_probe_finds_agents_skill_via_symlink(tmp_path: Path) -> None:
    """The layout that produced the bug: `.claude/skills` symlinked into `.agents`."""
    real = tmp_path / ".agents" / "skills" / "ship-it"
    _write(real / "SKILL.md")
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "ship-it").symlink_to(real, target_is_directory=True)

    stdout = _run(_prep_pr_probe(), tmp_path)
    assert ".claude/skills/ship-it/SKILL.md" in stdout
    assert ".agents/skills/ship-it/SKILL.md" in stdout
    # Both hits resolve to one physical file, so the operator can tell it is
    # one ship-it rather than two competing ones.
    resolved = {line.split(" -> ")[1] for line in stdout.strip().splitlines()}
    assert len(resolved) == 1, stdout


def test_prep_pr_probe_reports_command_and_skill_when_both_exist(
    tmp_path: Path,
) -> None:
    """The precedence branch: the probe must report both, not stop at the first."""
    _write(tmp_path / ".claude" / "commands" / "ship-it.md")
    _write(tmp_path / ".claude" / "skills" / "ship-it" / "SKILL.md")

    stdout = _run(_prep_pr_probe(), tmp_path)
    assert ".claude/commands/ship-it.md" in stdout
    assert ".claude/skills/ship-it/SKILL.md" in stdout
    resolved = {line.split(" -> ")[1] for line in stdout.strip().splitlines()}
    assert len(resolved) == 2, stdout


def test_prep_pr_probe_is_silent_when_no_ship_it_exists(tmp_path: Path) -> None:
    assert _run(_prep_pr_probe(), tmp_path).strip() == ""


def test_setup_probe_finds_skill_layout(tmp_path: Path) -> None:
    """/setup must not offer to bootstrap a command stub beside a real skill."""
    _write(tmp_path / ".claude" / "skills" / "ship-it" / "SKILL.md")

    assert ".claude/skills/ship-it/SKILL.md" in _run(_setup_probe(), tmp_path)


def test_setup_probe_is_silent_when_no_ship_it_exists(tmp_path: Path) -> None:
    assert _run(_setup_probe(), tmp_path).strip() == ""


def test_prep_pr_probe_separates_two_distinct_skill_files(tmp_path: Path) -> None:
    """The precondition Step 8's skill-vs-skill tie-break rests on.

    The symlink case collapses to one resolved path; two real files must NOT,
    or the tie-break bullet describes a state the probe can never report.
    """
    _write(tmp_path / ".claude" / "skills" / "ship-it" / "SKILL.md", "# claude\n")
    _write(tmp_path / ".agents" / "skills" / "ship-it" / "SKILL.md", "# agents\n")

    stdout = _run(_prep_pr_probe(), tmp_path)
    resolved = {line.split(" -> ")[1] for line in stdout.strip().splitlines()}
    assert len(resolved) == 2, stdout


def test_tie_break_prefers_the_runtime_loaded_skill_path() -> None:
    """The tie-break must keep naming a winner, and it must be the loaded path."""
    rule = _paragraph_at(PREP_PR_PATH, "**If both skill paths were found")
    assert ".claude/skills/ship-it/SKILL.md" in rule
    assert "prefer" in rule
