"""Guard tests: push the branch before /prep-pr's quality-gate window (#1414).

Pure-markdown assertions over the auto-dev finalize pipeline instruction file,
the prep-pr.md sync-with-base step, and the dispatch runbook doc. This repo
has an established convention (see ``tests/test_auto_dev_model_pins.py``,
``tests/test_auto_dev_preflight_resolutions.py``,
``tests/test_unavailability.py``, and
``tests/test_auto_dev_finalize_automerge_verification.py``) of a small
**private per-file** ``_cmd()``-style helper that reads
``.claude/commands/*.md`` prose and asserts substrings/regions, rather than a
shared ``conftest.py`` fixture. This file is the 5th consumer of that same
pattern.

Root cause pinned here (GEN-5343): finalize's Step 4c.2 checks out the
feature branch from origin, merges ``origin/main`` into it (a local merge
commit), and only THEN delegates to ``/prep-pr --skip-review``, whose own
``ship-it.md`` performs the first and only ``git push`` in the entire chain
— after `/prep-pr`'s quality-gate suite has already run for up to 5400s. If
the process is killed anywhere in that window, the impl commit plus the
merge-main commit exist only in the local worktree; nothing is on origin, so
a subsequent worktree reap destroys the work with no recoverable copy.

The fix pushes the branch at two additional sites — immediately after
``git merge origin/main --no-edit`` in ``auto-dev-finalize.md`` Step 4c.2,
and immediately after the equivalent merge in ``prep-pr.md`` Step 1 — using
the explicit-refspec push form and fetch+rev-parse verify idiom already
established elsewhere in this file family.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

from cw.auto_dev_result import _STAGE_REACHED_CANONICAL, StageReached

ROOT = Path(__file__).parent.parent
COMMANDS = ROOT / ".claude" / "commands"
DOCS = ROOT / "docs"


def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text()


def _doc(name: str) -> str:
    return (DOCS / name).read_text()


def _step4c2_section() -> str:
    content = _cmd("auto-dev-finalize.md")
    start = content.index("#### Step 4c.2 — spawn the agent")
    end = content.index("### Step 4c.5")
    return content[start:end]


def _prep_pr_step1_section() -> str:
    content = _cmd("prep-pr.md")
    start = content.index("## Step 1")
    end = content.index("## Step 2")
    return content[start:end]


_PUSH_RECIPE_RE = re.compile(
    r'git push origin HEAD:refs/heads/(?:"\$BRANCH"|<branch-name>)\n'
    r'\s*git fetch origin (?:"\$BRANCH"|<branch-name>)\n'
    r'\s*test "\$\(git rev-parse origin/(?:"\$BRANCH"|<branch-name>)\)"'
    r' = "\$\(git rev-parse HEAD\)"'
)


def _normalized_push_recipe(section: str) -> str:
    match = _PUSH_RECIPE_RE.search(section)
    assert match, "push+verify recipe not found in section"
    return match.group(0).replace('"$BRANCH"', "<branch-name>")


def test_step4c2_pushes_before_prep_pr_invocation() -> None:
    """The new push+verify block must appear after the pre-existing

    ``git merge origin/main --no-edit`` refresh, in file order, within the
    Step 4c.2 agent-prompt instructions (the fix for GEN-5343).
    """
    section = _step4c2_section()
    merge_idx = section.index("git merge origin/main --no-edit")
    push_idx = section.index("git push origin HEAD:refs/heads/")
    verify_idx = section.index("git rev-parse origin/")
    head_idx = section.index("git rev-parse HEAD")
    assert merge_idx < push_idx
    assert merge_idx < verify_idx
    assert merge_idx < head_idx


def test_step4c2_push_failure_blocks_before_invoking_prep_pr() -> None:
    section = _step4c2_section()
    assert "do NOT invoke `/prep-pr`" in section


def test_classifier_no_longer_claims_only_push_site() -> None:
    content = _cmd("auto-dev-finalize.md")
    assert "the only push site" not in content


def test_classifier_enumerates_all_push_sites() -> None:
    content = _cmd("auto-dev-finalize.md")
    assert "Step 4c.2" in content
    assert "Step 1 sync-with-base" in content
    assert "ship-it.md" in content


def test_no_new_stage_tag_introduced() -> None:
    finalize_content = _cmd("auto-dev-finalize.md")
    prep_pr_content = _cmd("prep-pr.md")
    assert "stage4c_pre_prep_pr_push" not in finalize_content
    assert "stage4c_pre_prep_pr_push" not in prep_pr_content


def test_stage_reached_literal_still_seven_canonical_values() -> None:
    assert set(typing.get_args(StageReached)) == _STAGE_REACHED_CANONICAL
    assert len(_STAGE_REACHED_CANONICAL) == 7


def test_prep_pr_step1_pushes_after_successful_merge() -> None:
    section = _prep_pr_step1_section()
    assert "git push origin HEAD:refs/heads/" in section
    assert "git rev-parse origin/" in section
    assert "git rev-parse HEAD" in section


def test_prep_pr_step1_push_failure_has_headless_block() -> None:
    section = _prep_pr_step1_section()
    assert "HEADLESS BLOCK" in section
    assert 'gate: "Step 1 sync-with-base push"' in section


def test_push_verify_recipe_matches_across_both_sites() -> None:
    """Guard against the two push+verify recipes silently diverging.

    Mirrors the classifier-signature PROSE MIRROR drift guard elsewhere in
    this file family (see `test_unavailability_signatures_mirrored_in_prose`):
    the push/fetch/verify command text must stay identical across
    auto-dev-finalize.md Step 4c.2 and prep-pr.md Step 1, modulo the
    `<branch-name>` vs `"$BRANCH"` placeholder substitution.
    """
    finalize_recipe = _normalized_push_recipe(_step4c2_section())
    prep_pr_recipe = _normalized_push_recipe(_prep_pr_step1_section())
    assert finalize_recipe == prep_pr_recipe


def test_dispatch_runbook_95_symptom_no_longer_unconditional() -> None:
    content = _doc("dispatch-runbook.md")
    assert "The branch is pushed to origin;" not in content
    symptom_start = content.index("### 9.5 Manual PR for a tombstoned")
    symptom_end = content.index("**Diagnose.**", symptom_start)
    symptom = content[symptom_start:symptom_end]
    assert "Step 4c.2" in symptom or "#1414" in symptom
