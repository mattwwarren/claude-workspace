"""Guard tests: FINALIZE's MUST_FIX Override Verification step (#2205).

A codex MUST_FIX park reached through ``cw dev-queue requeue --stage finalize``
arrives at FINALIZE with ``blocked_reason`` already cleared, so the stage itself
must re-check the worktree's verdict against the operator's override. These
tests pin the prose of ``.claude/commands/auto-dev-finalize.md`` and execute the
step's own resolver fence through the shared ``run_guard_fence`` runner, the
same way ``tests/test_auto_dev_finalize_semantic_resolve.py`` pins Step 4c.5.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.auto_dev_result.schema import FINALIZE_REGRESS_BLOCKER_REASONS
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from tests.conftest import (
    GUARD_FENCE_INVOKED,
    GUARD_MARKER_BAD_CASES,
    GUARD_MARKER_CURRENT,
    _bash_fences,
    _cmd,
    _placement,
    guard_candidate_path,
    run_guard_fence,
)

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

_SECTION_HEADING = "### MUST_FIX Override Verification (#2205)"
_NEXT_HEADING = "### Pre-Stage Detector Guard"
_SCRIPT = "check_must_fix_override.py"


def _finalize() -> str:
    return _cmd("auto-dev-finalize.md")


def _section() -> str:
    content = _finalize()
    start = content.index(_SECTION_HEADING)
    return content[start : content.index(_NEXT_HEADING, start)]


def _resolver_fence() -> str:
    fences = [
        fence
        for fence in _bash_fences(_section())
        if f"/.claude/scripts/{_SCRIPT}" in fence and "for candidate in" in fence
    ]
    assert len(fences) == 1, f"expected exactly one resolver fence, got {len(fences)}"
    return fences[0]


def _bullet(marker: str) -> str:
    bullets = [line for line in _section().splitlines() if marker in line]
    assert len(bullets) == 1, f"expected one bullet containing {marker!r}: {bullets}"
    return bullets[0]


def _run(
    tmp_path: Path,
    *,
    repo_local: str | None = None,
    global_copy: str | None = None,
    worktree_path_override: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_guard_fence(
        tmp_path,
        _resolver_fence(),
        _SCRIPT,
        repo_local=repo_local,
        global_copy=global_copy,
        worktree_path_override=worktree_path_override,
    )


def test_section_runs_before_the_pre_stage_detector_guard() -> None:
    """It gates PR reuse as well as PR creation, so it precedes both."""
    content = _finalize()
    stage4 = content.index("## Stage 4: PR Creation (Merge-Gated)")
    section = content.index(_SECTION_HEADING)
    guard = content.index(_NEXT_HEADING)
    assert stage4 < section < guard


@pytest.mark.parametrize("location", ["repo_local", "global_only"])
def test_fence_reaches_the_script_on_a_current_marker(
    tmp_path: Path, location: str
) -> None:
    result = _run(tmp_path, **_placement(location, GUARD_MARKER_CURRENT))
    assert result.returncode == 0, f"{location}: {result.stderr}"
    assert GUARD_FENCE_INVOKED in result.stdout
    assert guard_candidate_path(tmp_path, location, _SCRIPT) in result.stdout
    assert "STALE:" not in result.stdout


def test_fence_passes_verdict_context_and_head(tmp_path: Path) -> None:
    result = _run(tmp_path, repo_local=GUARD_MARKER_CURRENT)
    repo = tmp_path / "repo"
    assert f"--verdict {repo}/.claude/review-verdict.json" in result.stdout
    assert f"--context {repo}/.claude/cw-context.json" in result.stdout
    assert "--head" in result.stdout


def test_fence_resolves_the_cw_context_worktree(tmp_path: Path) -> None:
    """A planted cw-context.json worktree_path is the authoritative anchor."""
    result = _run(tmp_path, worktree_path_override=GUARD_MARKER_CURRENT)
    assert result.returncode == 0, result.stderr
    assert guard_candidate_path(tmp_path, "worktree_override", _SCRIPT) in result.stdout


@pytest.mark.parametrize("location", ["repo_local", "global_only"])
@pytest.mark.parametrize(("label", "script_body"), GUARD_MARKER_BAD_CASES)
def test_fence_hard_stops_on_a_stale_marker(
    tmp_path: Path, location: str, label: str, script_body: str
) -> None:
    result = _run(tmp_path, **_placement(location, script_body))
    assert result.returncode != 0, f"{location}/{label}: fence fell through"
    assert GUARD_FENCE_INVOKED not in result.stdout
    assert "STALE:" in result.stdout


def test_fence_skips_when_absent_from_both_locations(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    absent = _bullet("Absent from both locations")
    assert "check_must_fix_override: script absent, skipped" in absent


def test_stale_marker_bullet_is_a_headless_block() -> None:
    stale = _bullet("HEADLESS BLOCK")
    assert _SCRIPT in stale
    assert "need >= 1" in stale
    assert '"agent_block"' in stale


def test_blocked_exit_parks_with_codex_must_fix_findings() -> None:
    """The FINALIZE-side park reuses the Stage-3 reason, not agent_block."""
    blocked = _bullet("**Exit 1")
    assert f'blocker.reason: "{CODEX_MUST_FIX_FINDINGS}"' in blocked
    assert 'blocker.reason: "agent_block"' not in blocked
    assert "--override-must-fix" in blocked
    assert "--stage finalize" in blocked


def test_codex_must_fix_findings_never_self_heals() -> None:
    """Rule 5a regresses only FINALIZE_REGRESS_BLOCKER_REASONS to IMPL; a
    MUST_FIX park must reach the operator instead."""
    assert CODEX_MUST_FIX_FINDINGS not in FINALIZE_REGRESS_BLOCKER_REASONS
    assert "FINALIZE_REGRESS_BLOCKER_REASONS" in _section()


def test_overridden_exit_continues_and_renders_operator_override() -> None:
    overridden = _bullet("**Exit 0")
    assert '"overridden"' in overridden
    assert "## Operator override" in overridden
    content = _finalize()
    step4d = content[content.index("### Step 4d: Post-Ship Pipeline Bookkeeping") :]
    rendering = step4d[step4d.index("## Operator override") :]
    for field in ("actor", "reason", "reviewed_sha"):
        assert field in rendering[:1500]


def test_orientation_no_longer_claims_a_nonexistent_check() -> None:
    """The orientation paragraph names the real authority for the halt/ship
    decision instead of an unspecified "review-completeness" check."""
    orientation = _finalize().split("## Resolve carried-through context", 1)[0]
    assert "review-completeness checks" not in orientation
    assert "MUST_FIX Override Verification" in orientation
