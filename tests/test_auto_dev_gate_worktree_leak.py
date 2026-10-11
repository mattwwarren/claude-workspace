"""Guard tests: gate worktree setup must not leak (#1443).

Covers the Step 2.5 / Mitigation 1 gate-worktree block in both
`auto-dev-impl.md` and `auto-dev.md`:
- path keyed on the gate key `gate_key.py` prints (clone identity plus
  branch; deterministic), not $CW_SESSION and not $$ (PID, unreclaimable)
  (#2436)
- the gate key is validated as 16 lowercase hex before any path is built
- cleanup trap covers EXIT, and INT/TERM additionally force an explicit exit
  (a trap alone only runs cleanup and resumes execution — it does not stop
  the script)
- `git worktree add` exit status is checked (not silently ignored)
- a stale worktree entry from a prior killed invocation is self-healed
- gate-failure prose covers a gate-setup failure, not just checks 1/3/4
"""

from tests.conftest import _cmd


def test_impl_gate_worktree_path_keyed_on_gate_key_not_session() -> None:
    """auto-dev-impl.md: TMPWT/TMPDIR keyed on $GATE_KEY, never bare $$."""
    content = _cmd("auto-dev-impl.md")
    assert '"${CW_GATE_ROOT:-/var/tmp}/cw-gate-wt-$GATE_KEY"' in content
    assert '"${CW_GATE_ROOT:-/var/tmp}/cw-gate-tmp-$GATE_KEY"' in content
    assert '"/tmp/gate-wt-$$"' not in content


def test_auto_dev_gate_worktree_path_keyed_on_gate_key_not_session() -> None:
    """auto-dev.md: TMPWT/TMPDIR keyed on $GATE_KEY, never bare $$."""
    content = _cmd("auto-dev.md")
    assert '"${CW_GATE_ROOT:-/var/tmp}/cw-gate-wt-$GATE_KEY"' in content
    assert '"${CW_GATE_ROOT:-/var/tmp}/cw-gate-tmp-$GATE_KEY"' in content
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


def test_impl_gate_worktree_gate_key_guarded() -> None:
    """auto-dev-impl.md: the gate key is validated; no $CW_SESSION guard remains."""
    content = _cmd("auto-dev-impl.md")
    assert '[[ "$GATE_KEY" =~ ^[0-9a-f]{16}$ ]]' in content
    assert ': "${CW_SESSION:?CW_SESSION must be set}"' not in content


def test_auto_dev_gate_worktree_gate_key_guarded() -> None:
    """auto-dev.md: the gate key is validated; no $CW_SESSION guard remains."""
    content = _cmd("auto-dev.md")
    assert '[[ "$GATE_KEY" =~ ^[0-9a-f]{16}$ ]]' in content
    assert ': "${CW_SESSION:?CW_SESSION must be set}"' not in content


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
