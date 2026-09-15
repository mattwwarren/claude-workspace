"""Doc-structure guards for the Pre-mutation guard's script resolution (#2141).

Mirrors ``tests/test_impl_guard_staleness_docs.py``'s shape: a section helper
over the prose that owns the call site, plus substring assertions. The
Pre-mutation guard has no ``##``/``###`` heading of its own (it is a bullet
inside the Stage 2 agent's prompt list), so the section is delimited by its
own bullet text and the next sibling bullet.

``check_not_main_checkout.py`` is the one guard script with no dedicated
behaviour-test file, so its ``# cw-script-version`` header assertion lives
here rather than in a script-test sibling.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import _cmd

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "check_not_main_checkout.py"

_SECTION_START = "- **Pre-mutation guard (hard, not prose) — #766:**"
_SECTION_END = "\n- Instruction: if anything fails"

_MARKER = "cw-script-version"


def _pre_mutation_guard_section() -> str:
    content = _cmd("auto-dev-impl.md")
    start = content.index(_SECTION_START)
    end = content.index(_SECTION_END, start)
    return content[start:end]


def _marker_windows(section: str, span: int = 200) -> list[str]:
    """Every ±*span*-char window around a ``cw-script-version`` occurrence."""
    windows: list[str] = []
    idx = section.find(_MARKER)
    while idx != -1:
        windows.append(section[max(0, idx - span) : idx + span])
        idx = section.find(_MARKER, idx + 1)
    return windows


def test_pre_mutation_guard_resolves_repo_local_then_global_path() -> None:
    """Repo-local first, then the globally installed copy (#2096 ships it).

    The repo-local candidate is asserted in its ``$GUARD_ROOT``-anchored
    spelling, not as a bare substring: a cwd-relative probe would satisfy the
    looser assertion while silently missing the repo-local copy whenever the
    cwd is not the worktree root (review round 2).
    """
    section = _pre_mutation_guard_section()
    assert '"$GUARD_ROOT/.claude/scripts/check_not_main_checkout.py"' in section
    assert '"$HOME/.claude/scripts/check_not_main_checkout.py"' in section
    assert "for candidate in .claude/scripts/" not in section


def test_pre_mutation_guard_absent_message_unchanged() -> None:
    """Absent from BOTH locations keeps its pre-existing non-blocking skip.

    Regression: the resolver rewrite must not repurpose this message for the
    new marker-stale branch, which is a hard stop instead.
    """
    section = _pre_mutation_guard_section()
    assert "check_not_main_checkout: script absent, skipped" in section


def test_pre_mutation_guard_still_blocks_on_genuine_failure() -> None:
    """A real non-zero exit from a found-and-current script still blocks."""
    section = _pre_mutation_guard_section()
    assert 'blocker.reason: "impl_failed"' in section
    assert "check_not_main_checkout exited" in section


def test_pre_mutation_guard_greps_cw_script_version_marker() -> None:
    """Existence alone is insufficient — the resolved copy must be verified."""
    assert _MARKER in _pre_mutation_guard_section()


def test_pre_mutation_guard_headless_blocks_on_stale_marker() -> None:
    """A missing/stale marker is a HARD stop, never a skip-and-continue."""
    windows = _marker_windows(_pre_mutation_guard_section())
    assert windows
    assert any(
        "HEADLESS BLOCK" in window
        and ("missing" in window.lower() or "stale" in window.lower())
        for window in windows
    )


def test_pre_mutation_guard_stale_marker_pins_blocker_disposition() -> None:
    """Not just the words "HEADLESS BLOCK" — the actual disposition an agent
    must emit, mirroring gate 2's equivalent pin (#2141 review round 2)."""
    section = _pre_mutation_guard_section()
    windows = _marker_windows(section, span=400)
    blocking = [window for window in windows if "HEADLESS BLOCK" in window]
    assert blocking
    disposition = "\n".join(blocking)
    assert 'blocker.reason: "impl_failed"' in disposition
    assert (
        "HEADLESS BLOCK: check_not_main_checkout.py at <resolved-path>" in disposition
    )
    assert "missing/stale cw-script-version marker (need >= 1)" in disposition
    assert "STOP" in section


def test_check_not_main_checkout_declares_cw_script_version_header() -> None:
    """Line 2 (index 1), directly under the shebang, above the docstring."""
    lines = _SCRIPT.read_text(encoding="utf-8").splitlines()
    assert lines[1] == "# cw-script-version: 1"
