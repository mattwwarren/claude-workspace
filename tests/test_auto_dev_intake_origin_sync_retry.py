"""Guard tests: bounded wait-and-recheck for behind-only origin sync (#1434).

Pure-markdown assertions over the auto-dev-intake pipeline instruction file's
"Pre-flight: Origin Sync Check" section. This repo has an established
convention (see ``tests/test_auto_dev_finalize_early_push.py`` and its own
list of prior consumers) of a small **private per-file** ``_cmd()``-style
helper that reads ``.claude/commands/*.md`` prose and asserts
substrings/regions, rather than a shared ``conftest.py`` fixture. This file
is the 6th consumer of that same pattern.

Root cause pinned here: `/auto-dev-intake`'s Step P3 (headless) exited the
`blocked` sentinel unconditionally and immediately on any divergence between
local `main` and `origin/main` — including the transient behind-only case
where a concurrent wave PR merge is in the process of being auto-ff'd into
the base checkout's `main` by `_resolve_freshness`
(``src/cw/dispatch/gating.py``). That race meant the next queued ticket
would spuriously block at pre-flight with `local_main_diverged_from_origin`
even though the divergence was about to resolve itself on its own, requiring
a manual `refresh-all`.

The fix adds a bounded wait-and-recheck loop scoped to the behind-only case
(``AHEAD == 0`` and ``BEHIND > 0``): poll local `main` against the
already-fetched `ORIGIN_MAIN` a few times (no re-fetch, no ref mutation)
before falling through to the pre-existing blocked sentinel.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
COMMANDS = ROOT / ".claude" / "commands"


def _cmd() -> str:
    return (COMMANDS / "auto-dev-intake.md").read_text()


def _appendix() -> str:
    return (COMMANDS / "auto-dev-intake-appendix.md").read_text()


def _origin_sync_section() -> str:
    """The Origin Sync Check's divergence-handling block, Step P2 through P3.

    #1879 relocated Steps P2 and P3 out of ``auto-dev-intake.md`` and into its
    companion appendix: Step P1 (fetch and compare) runs on every invocation
    and stays in the core doc, but the divergence branch below it fires only
    when local ``main`` has actually drifted from ``origin/main``, so it is
    rare-path content. Every assertion in this file is re-pointed at the new
    location; none was dropped or loosened.
    """
    content = _appendix()
    start = content.index("## Origin Sync Check divergence handling")
    end = content.index("\n## ", start)
    return content[start:end]


def test_core_doc_keeps_step_p1_and_the_appendix_trigger() -> None:
    """Detection stays on the common path; only the divergence branch moved."""
    core = _cmd()
    assert "### Step P1: Fetch and compare" in core
    assert "Origin Sync Check divergence handling (Steps P2 and P3)" in core
    assert "### Step P2 (interactive)" not in core
    assert "### Step P3 (headless)" not in core


def _step_p2_section() -> str:
    content = _origin_sync_section()
    start = content.index("### Step P2 (interactive)")
    end = content.index("### Step P3 (headless)")
    return content[start:end]


def _step_p3_section() -> str:
    content = _origin_sync_section()
    start = content.index("### Step P3 (headless)")
    return content[start:]


def _behind_only_wait_block() -> str:
    """Isolated span for the new retry logic only — must NOT bleed into the
    pre-existing 'Producer note' paragraph, which legitimately contains the
    literal string 'git pull --ff-only' describing human recovery, not
    worker action.
    """
    section = _step_p3_section()
    start = section.index("#### Behind-only wait-and-recheck")
    end = section.index("EXIT with the structured `blocked` sentinel")
    return section[start:end]


def test_wait_and_recheck_text_present_only_in_step_p3() -> None:
    assert "Behind-only wait-and-recheck" in _step_p3_section()
    assert "Behind-only wait-and-recheck" not in _step_p2_section()


def test_step_p2_sync_now_bullet_unchanged() -> None:
    expected = (
        '| Sync now | If ahead-only: `git -C "$REPO" push origin main`. '
        'If behind-only: `git -C "$REPO" pull --ff-only`.'
    )
    assert expected in _step_p2_section()


def test_behind_only_is_the_only_wait_gated_case() -> None:
    section = _step_p3_section()
    assert "AHEAD == 0" in section
    wait_idx = section.index("#### Behind-only wait-and-recheck")
    sentinel_idx = section.index("EXIT with the structured `blocked` sentinel")
    assert wait_idx < sentinel_idx


def test_sentinel_json_reason_field_unchanged() -> None:
    assert '"reason": "local_main_diverged_from_origin"' in _step_p3_section()


@pytest.mark.parametrize(
    "forbidden",
    [
        "git push",
        "git pull",
        "git reset",
        "git merge",
        "git fetch origin main:main",
    ],
)
def test_no_ref_mutating_git_commands_in_retry_logic(forbidden: str) -> None:
    assert forbidden not in _behind_only_wait_block()


def test_wait_block_only_reads_local_ref_no_refetch() -> None:
    block = _behind_only_wait_block()
    assert 'git -C "$REPO" rev-parse main' in block
    assert 'git -C "$REPO" fetch' not in block


def test_cadence_constants_match_tick_interval_and_stale_convention() -> None:
    block = _behind_only_wait_block()
    assert "30" in block
    assert "60" in block
    assert "90" in block
