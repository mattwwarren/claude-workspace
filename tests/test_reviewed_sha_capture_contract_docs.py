"""Guard tests: the Claude-native review path states the ``reviewed_sha``
capture contract (#2123).

Pure-markdown assertions over ``.claude/commands/auto-dev-review.md``,
following the ``_cmd()`` + section-window convention of
``test_sentinel_review_counters_docs.py``. ``_cmd`` is imported from
``tests.conftest`` (#1787); ``_after``/``_nearby`` are imported from
``test_auto_dev_preflight_resolutions`` rather than duplicated.

Background: dispatch re-gates a REVIEW-stage sentinel to
``review_pending_approval`` (disposition ``review_artifacts_stale``) when
``review.reviewed_sha`` disagrees with the worktree's live HEAD. That gate is
only useful if the producer stamps the sha it *actually verified*. The two
non-Claude executors get this structurally — Codex re-derives ``reviewed_sha``
fresh on every re-review cycle, OpenCode captures it at the tail of its single
fix step, and ``tests/test_opencode_runner.py`` pins the OpenCode wording. The
Claude-native path is the one that needs a documented mechanism, because its
``cw review consolidate`` call freezes a *pre-fix* Checkpoint-3a sha: a
separate ``REVIEWED_SHA_FOR_SENTINEL`` variable is initialised to that pre-fix
default and overwritten at Step 3c once ``cw review verify-fixes`` completes.

These tests pin the three load-bearing halves of that contract — the pre-fix
init, the post-verification overwrite, and the sentinel sourcing — so a future
edit cannot quietly move the capture point back before the fix loop (which
would mismatch HEAD on every round that fixed anything) or drop the field from
the sentinel template (which reads to the gate as "never stamped").
"""

from tests.conftest import _cmd
from tests.test_auto_dev_preflight_resolutions import _after, _nearby

FREEZE_ANCHOR = "**Freeze** `.review.must_fix_initial`"
CHECKPOINT_3A_SHA_ANCHOR = 'Capture `CHECKPOINT_3A_SHA="<HEAD sha>"`'
OVERWRITE_ANCHOR = 'set `REVIEWED_SHA_FOR_SENTINEL="$FIX_TIP_SHA"`'
SENTINEL_SOURCING_ANCHOR = "**`review.reviewed_sha` is also not one of the frozen three"


def _checkpoint_3a_section() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index("### Checkpoint 3a: Adjudicate every finding")
    end = content.index("### Step 3b:")
    return content[start:end]


def _step_3c_section() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index("### Step 3c: Verify the `fixed` claims")
    end = content.index("## Stage 3 Completion (headless only)")
    return content[start:end]


def _stage3_completion_section() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index("## Stage 3 Completion (headless only)")
    return content[start:]


def test_checkpoint_3a_initializes_reviewed_sha_as_the_pre_fix_default() -> None:
    """Checkpoint 3a step 2 seeds ``REVIEWED_SHA_FOR_SENTINEL`` from the
    pre-fix Checkpoint-3a sha and points forward at Step 3c (#2123).

    The default exists so a session that somehow reaches the completion
    sentinel without Step 3c stamps a sha it can substantiate rather than an
    empty field; naming Step 3c here is what stops a reader treating the
    pre-fix value as the final answer.
    """
    window = _after(_checkpoint_3a_section(), CHECKPOINT_3A_SHA_ANCHOR, span=600)
    assert 'REVIEWED_SHA_FOR_SENTINEL="$CHECKPOINT_3A_SHA"' in window
    assert "pre-fix default" in window
    assert "Step 3c" in window


def test_freeze_rule_excludes_reviewed_sha_from_the_frozen_set() -> None:
    """``review.reviewed_sha`` is explicitly carved out of the Checkpoint-3a
    frozen block (#2123).

    Every other ``.review`` counter is frozen from the *first* consolidate
    call. Freezing ``reviewed_sha`` the same way would pin it to the pre-fix
    sha by construction, so the carve-out is the contract, not a footnote.
    """
    window = _after(_checkpoint_3a_section(), FREEZE_ANCHOR, span=2000)
    assert "`review.reviewed_sha` is explicitly NOT part of this frozen set" in window
    assert "REVIEWED_SHA_FOR_SENTINEL" in window
    assert "Step 3c" in window


def test_step_3c_captures_head_after_the_fix_loop() -> None:
    """Step 3c derives the post-fix sha from the fetched branch tip, not from
    a consolidate-call envelope (#2123).

    ``git rev-parse origin/<branch-name>`` after ``git fetch`` is what makes
    the capture point correct: it runs once, unconditionally, after every fix
    cycle (or the sparse-feedback decision that none would run) has settled.
    """
    section = _step_3c_section()
    assert "git fetch origin <branch-name>" in section
    assert 'FIX_TIP_SHA="$(git rev-parse origin/<branch-name>)"' in section


def test_step_3c_overwrites_reviewed_sha_only_on_verify_fixes_success() -> None:
    """The overwrite is gated on ``cw review verify-fixes`` exiting 0 (#2123).

    A branch tip whose fix claims were never verified is exactly the state the
    dispatch-side gate exists to catch, so the doc must not let a session stamp
    it as reviewed.
    """
    section = _step_3c_section()
    window = _nearby(section, OVERWRITE_ANCHOR, span=120) + _after(
        section, OVERWRITE_ANCHOR, span=700
    )
    assert "On success (exit 0)" in window
    assert "`verify-fixes` completed" in window
    assert "post-fix HEAD when the fix loop ran" in window
    assert "review.reviewed_sha" in window


def test_stage3_completion_sources_reviewed_sha_from_step_3c() -> None:
    """The completion sentinel's ``review.reviewed_sha`` is sourced from
    ``REVIEWED_SHA_FOR_SENTINEL``, never from a consolidate call (#2123).
    """
    window = _after(_stage3_completion_section(), SENTINEL_SOURCING_ANCHOR, span=900)
    assert "Source it from `REVIEWED_SHA_FOR_SENTINEL` (Step 3c)" in window
    assert "once the fix loop's fix claims have been verified" in window
    assert "unchanged Checkpoint-3a sha when no fix cycle ran" in window


def test_sentinel_template_carries_the_reviewed_sha_placeholder() -> None:
    """The template names the field and its source (#2123).

    Prose alone is not enough: the Stage 3 sentinel is emitted by copying this
    template, so a field absent from it is a field the producer has no slot to
    fill — which the gate reads as "never stamped" and fails closed on.
    """
    section = _stage3_completion_section()
    assert '"reviewed_sha": "<REVIEWED_SHA_FOR_SENTINEL from Step 3c>"' in section
    window = _nearby(section, '"reviewed_sha":', span=250)
    assert '"agents_run"' in window
