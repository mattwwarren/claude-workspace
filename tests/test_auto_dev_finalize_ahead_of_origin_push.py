"""Guard tests: finalize never resets over unpushed local commits (#2354).

Pure-markdown assertions over ``auto-dev-finalize.md`` Step 4c.2, sibling of
``test_auto_dev_finalize_early_push.py`` and sharing its hoisted
``_step4c2_section`` reader from ``tests/conftest.py``.

Root cause pinned here (#2216): Step 4c.2 ran ``git fetch origin`` then
``git checkout -B <branch-name> origin/<branch-name>`` unconditionally, a
force-reset of the local branch to origin's tip. A codex fix-loop commit that
existed only locally was silently discarded by that reset, before the later
push-after-merge could ever carry it. The fix pushes any commits local HEAD
has beyond ``origin/<branch-name>`` BEFORE the reset, and BLOCKs rather than
resetting when that push fails.
"""

from __future__ import annotations

from tests.conftest import _step4c2_section

_RESET = "git checkout -B <branch-name> origin/<branch-name>"
_AHEAD_COUNT = 'git rev-list --count "origin/<branch-name>..HEAD"'
_AHEAD_PUSH = "if ! git push origin HEAD:refs/heads/<branch-name>; then"


def test_step4c2_checks_ahead_commits_before_reset() -> None:
    section = _step4c2_section()
    fetch_idx = section.index("git fetch origin\n")
    ahead_idx = section.index(_AHEAD_COUNT)
    push_idx = section.index(_AHEAD_PUSH)
    reset_idx = section.index(_RESET)
    assert fetch_idx < ahead_idx < push_idx < reset_idx


def test_step4c2_reset_comment_no_longer_claims_unconditional_safety() -> None:
    stale = (
        "The reset still pulls any fix-loop pushes to origin/<branch>\n"
        "  # not yet reflected locally"
    )
    section = _step4c2_section()
    assert stale not in section
    assert "not yet reflected locally (#1047)" not in section


def test_step4c2_diverged_ahead_push_blocks_not_resets() -> None:
    section = _step4c2_section()
    push_idx = section.index(_AHEAD_PUSH)
    reset_idx = section.index(_RESET)
    failure_branch = section[push_idx:reset_idx]
    assert "BLOCK" in failure_branch
    assert "git log --oneline origin/<branch-name>..HEAD" in failure_branch
    assert "exit 1" in failure_branch
    assert 'blocker.reason: "agent_block"' in section
    assert "never discard" in section.lower()
