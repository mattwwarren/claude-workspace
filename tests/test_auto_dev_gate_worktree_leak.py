"""Guard tests: gate worktree setup must not leak (#1443).

Covers the Step 2.5 / Mitigation 1 gate-worktree block in both
`auto-dev-impl.md` and `auto-dev.md`:
- path keyed on $CW_SESSION (deterministic), not $$ (PID, unreclaimable)
- $CW_SESSION is guarded against being unset/empty
- cleanup trap covers EXIT, and INT/TERM additionally force an explicit exit
  (a trap alone only runs cleanup and resumes execution — it does not stop
  the script)
- `git worktree add` exit status is checked (not silently ignored)
- a stale worktree entry from a prior killed invocation is self-healed
- gate-failure prose covers a gate-setup failure, not just checks 1/3/4
"""

from tests.conftest import _cmd


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
    """auto-dev-impl.md: INT/TERM trap must cleanup AND force an explicit exit."""
    content = _cmd("auto-dev-impl.md")
    assert "trap gate_wt_cleanup EXIT" in content
    assert "trap 'gate_wt_cleanup; exit 143' INT TERM" in content
    assert "2>/dev/null' EXIT\n" not in content
    assert "2>/dev/null' EXIT INT TERM" not in content


def test_auto_dev_gate_worktree_trap_covers_int_and_term() -> None:
    """auto-dev.md: INT/TERM trap must cleanup AND force an explicit exit."""
    content = _cmd("auto-dev.md")
    assert "trap gate_wt_cleanup EXIT" in content
    assert "trap 'gate_wt_cleanup; exit 143' INT TERM" in content
    assert "2>/dev/null' EXIT\n" not in content
    assert "2>/dev/null' EXIT INT TERM" not in content


def test_impl_gate_worktree_session_var_guarded() -> None:
    """auto-dev-impl.md: TMPWT derivation must guard against unset $CW_SESSION."""
    content = _cmd("auto-dev-impl.md")
    assert ': "${CW_SESSION:?CW_SESSION must be set}"' in content


def test_auto_dev_gate_worktree_session_var_guarded() -> None:
    """auto-dev.md: TMPWT derivation must guard against unset $CW_SESSION."""
    content = _cmd("auto-dev.md")
    assert ': "${CW_SESSION:?CW_SESSION must be set}"' in content


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
