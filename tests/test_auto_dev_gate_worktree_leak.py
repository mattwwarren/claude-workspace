"""Guard tests: gate worktree setup must not leak (#1443).

Covers the Step 2.5 / Mitigation 1 gate-worktree block in both
`auto-dev-impl.md` and `auto-dev.md`:
- path keyed on $CW_SESSION (deterministic), not $$ (PID, unreclaimable)
- trap covers EXIT INT TERM, not just EXIT
- `git worktree add` exit status is checked (not silently ignored)
- a stale worktree entry from a prior killed invocation is self-healed
- gate-failure prose covers a gate-setup failure, not just checks 1/3/4
"""

from pathlib import Path

COMMANDS = Path(__file__).parent.parent / ".claude" / "commands"


def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text()


def test_impl_gate_worktree_path_keyed_on_session_not_pid() -> None:
    """auto-dev-impl.md: TMPWT must be keyed on $CW_SESSION, never bare $$."""
    content = _cmd("auto-dev-impl.md")
    assert '"/tmp/gate-wt-$CW_SESSION"' in content
    assert '"/tmp/gate-wt-$$"' not in content


def test_auto_dev_gate_worktree_path_keyed_on_session_not_pid() -> None:
    """auto-dev.md: TMPWT must be keyed on $CW_SESSION, never bare $$."""
    content = _cmd("auto-dev.md")
    assert '"/tmp/gate-wt-$CW_SESSION"' in content
    assert '"/tmp/gate-wt-$$"' not in content


def test_impl_gate_worktree_trap_covers_int_and_term() -> None:
    """auto-dev-impl.md: cleanup trap must cover EXIT INT TERM, not bare EXIT."""
    content = _cmd("auto-dev-impl.md")
    assert content.count("EXIT INT TERM") >= 1
    assert "2>/dev/null' EXIT\n" not in content


def test_auto_dev_gate_worktree_trap_covers_int_and_term() -> None:
    """auto-dev.md: cleanup trap must cover EXIT INT TERM, not bare EXIT."""
    content = _cmd("auto-dev.md")
    assert content.count("EXIT INT TERM") >= 1
    assert "2>/dev/null' EXIT\n" not in content


def test_impl_gate_worktree_add_checks_exit_status() -> None:
    """auto-dev-impl.md: `git worktree add` failure must abort, not run gates."""
    content = _cmd("auto-dev-impl.md")
    assert 'git worktree add --detach "$TMPWT" origin/<branch-name> || {' in content


def test_auto_dev_gate_worktree_add_checks_exit_status() -> None:
    """auto-dev.md: `git worktree add` failure must abort, not run gates."""
    content = _cmd("auto-dev.md")
    assert 'git worktree add --detach "$TMPWT" origin/<branch-name> || {' in content


def test_impl_gate_worktree_self_heals_stale_entry() -> None:
    """auto-dev-impl.md: gate setup must prune/remove a stale entry before re-adding."""
    content = _cmd("auto-dev-impl.md")
    assert "git worktree prune" in content


def test_auto_dev_gate_worktree_self_heals_stale_entry() -> None:
    """auto-dev.md: gate setup must prune/remove a stale entry before re-adding."""
    content = _cmd("auto-dev.md")
    assert "git worktree prune" in content


def test_impl_gate_failure_prose_covers_setup_failure() -> None:
    """auto-dev-impl.md: 'On gate failure' heading must cover gate-setup failure too."""
    content = _cmd("auto-dev-impl.md")
    assert (
        "**On gate failure (gate setup itself — e.g. `git worktree add` erroring"
        in content
    )


def test_auto_dev_gate_failure_prose_covers_setup_failure() -> None:
    """auto-dev.md: 'Headless behavior' sentence must cover gate-setup failure too."""
    content = _cmd("auto-dev.md")
    assert "or gate setup itself — e.g. `git worktree add` erroring" in content
