"""Doc-structure guards for the Stage 2 impl prompt's three post-gate checks (#2084).

The Stage 2 impl agent is told to run three checks after the type-check gate:
an override-set mypy sweep (a scoped mypy run cannot see a subclass override in
another file), a per-guard kill-check (revert the line, confirm the focused test
goes red, restore), and a sensitive-carrier grep over the changed files. The
prose is the only contract: nothing parses it, so these tests pin the literal
strings the agent must read, the restore and no-amend discipline, and the
sensitive-carrier regex itself (run for real under ERE against a tmp file).

Sibling of `tests/test_mypy_baseline_gate_docs.py`; helpers come from
`tests/_shared/workflow_text.py` and nothing is copied here.
"""

import re
import subprocess
from pathlib import Path

from tests._shared.workflow_text import (
    _REPO_ROOT,
    _appendix,
    _cmd,
    _norm,
    _section_between,
)

_WINDOW_START = "**Type check gate"
_WINDOW_END = "Instruction to read model/schema definitions"
_OVERRIDE = "**Override sweep.**"
_KILL = "**Kill-check.**"
_CARRIER = "**Sensitive-carrier grep.**"
_GREP_PATTERN = re.compile(r"grep -nE '([^']+)'")


def _window() -> str:
    """The three new bullets sit between the type-check gate and the neighbor bullet."""
    return _norm(_section_between(_cmd("auto-dev-impl.md"), _WINDOW_START, _WINDOW_END))


def test_three_bullets_follow_the_type_check_gate_in_order() -> None:
    """Each bullet appears once, in order, after the gate's last sub-bullet."""
    window = _window()
    gate_end = window.index("those are your errors too")
    positions = [window.index(bullet) for bullet in (_OVERRIDE, _KILL, _CARRIER)]
    for bullet in (_OVERRIDE, _KILL, _CARRIER):
        assert window.count(bullet) == 1, bullet
    assert gate_end < positions[0] < positions[1] < positions[2]


def test_override_sweep_names_the_override_package_and_forbids_baselining() -> None:
    """The sweep names the overrides package and bars baselining its error."""
    section = _section_between(_window(), _OVERRIDE, _KILL)
    for literal in (
        "package",
        "[override]",
        "Never baseline",
        "STOP and report a BLOCK",
    ):
        assert literal in section, literal
    # Consistency: the sweep restates the appendix's existing no-growth rule.
    assert "The baseline may never grow in this diff." in _norm(_appendix("impl"))


def test_kill_check_mutations_and_reporting() -> None:
    """The kill-check names its mutations, the friction key and the carrier rule."""
    section = _section_between(_window(), _KILL, _CARRIER)
    literals = (
        "goes RED",
        "revert that line",
        "change the argument to `[]`",
        "replace the logger with a no-op",
        "kill_checks",
        "Kill-checks run:",
        "own worktree",
        '"target"',
        '"mutation"',
        '"test_file"',
        '"red_confirmed"',
        '"restored"',
        "body of the NEXT commit",
        "friction report's `kill_checks` array",
        "authoritative record",
    )
    for literal in literals:
        assert literal in section, literal
    assert "commit that adds the guard" not in section


def test_kill_check_restore_is_clean_never_stashes_and_never_amends() -> None:
    """Restore is `git checkout --`, a clean-tree check, and no stash or amend."""
    section = _section_between(_window(), _KILL, _CARRIER)
    for literal in (
        "git checkout -- <file>",
        "git status --porcelain",
        "never `git stash`",
        "Never amend a commit",
        "git commit --amend",
    ):
        assert literal in section, literal
    # The only mention of stash is the prohibition itself.
    assert section.count("git stash") == 1


def test_sensitive_carrier_grep_command_is_verbatim() -> None:
    """The grep is pinned verbatim and carries the next-commit-or-friction rule."""
    section = _section_between(_window(), _CARRIER)
    assert (
        "grep -nE 'str\\(exc|repr\\(exc|exc_info=True|logger\\.exception|%r|!r\\}'"
        in section
    )
    assert "friction report" in section
    assert "NEXT commit" in section
    assert "in the commit body" not in section


def test_sensitive_carrier_regex_hits_each_carrier_and_only_those(
    tmp_path: Path,
) -> None:
    """Run the doc's own regex under ERE: carriers match, decoys do not."""
    section = _section_between(_window(), _CARRIER)
    match = _GREP_PATTERN.search(section)
    assert match is not None, "sensitive-carrier grep pattern not found in the doc"
    pattern = match.group(1)

    lines = [
        "raise ValueError(str(exc))",  # 1 carrier: str(exc
        "msg = repr(exc)",  # 2 carrier: repr(exc
        'logger.error("x", exc_info=True)',  # 3 carrier: exc_info=True
        'logger.exception("boom")',  # 4 carrier: logger.exception
        'print("value %r" % value)',  # 5 carrier: %r
        'print(f"{x!r}")',  # 6 carrier: !r}
        'print("hello")',  # 7 decoy
        "x = str(value)",  # 8 decoy: not str(exc
        'log.info("ok")',  # 9 decoy
        'exc_text = "clean"',  # 10 decoy
    ]
    target = tmp_path / "carriers.py"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = subprocess.run(
        ["grep", "-nE", pattern, str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    hit_lines = [int(row.split(":", 1)[0]) for row in result.stdout.splitlines()]
    assert hit_lines == [1, 2, 3, 4, 5, 6]


def test_kill_checks_lives_in_the_impl_bullet_not_the_shared_friction_block() -> None:
    """The shared Friction Protocol and Completion Artifacts stay untouched."""
    assert "kill_checks" not in _cmd("auto-dev.md")
    completion = _section_between(
        _cmd("auto-dev-impl.md"),
        "## Completion Artifacts (required for",
        "Hallucinating or paraphrasing these",
    )
    assert "kill_checks" not in completion


def test_reviewer_kill_check_rule_is_unchanged() -> None:
    """Reviewers still request kill-checks and never run them in the shared worktree."""
    assert "NOT yours to run here" in _norm(_cmd("auto-dev-review.md"))
    reviewer = (_REPO_ROOT / ".claude" / "agents" / "test-reviewer.md").read_text(
        encoding="utf-8"
    )
    assert "Never mutate the checkout under review." in _norm(reviewer)
