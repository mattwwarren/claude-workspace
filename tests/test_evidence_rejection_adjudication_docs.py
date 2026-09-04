"""Guard tests: an unmatched evidence quote is adjudicated, not dropped (#2099).

Pure-markdown assertions over `/auto-dev-review`'s Checkpoint 3a rules, mirroring
the ``read_text()`` + literal-substring/window convention of
``test_plan_persistence.py`` / ``test_auto_dev_preflight_resolutions.py``.
``_cmd`` is imported from ``tests.conftest`` (#1787); ``_after``/``_nearby`` are
imported from ``test_auto_dev_preflight_resolutions`` rather than duplicated,
since that file already defines and exports them.

Background: `cw review consolidate` used to reject a finding whose `evidence`
quote did not match its declared diff window as `evidence_not_in_diff`, and the
prompt then discarded it (below MUST_FIX) or turned it into an operator park
(at MUST_FIX) — in both cases without anyone judging the claim. Over a single
ticket that discarded correct findings at least four times, twice hiding a live
production bug. The root cause of the round-3 occurrence was a `PostToolUse`
formatter hook rewriting the file after the reviewer authored its quote, so the
quote and the diff carried the same code wrapped differently.

The code half of the fix routes that verdict into `.accepted` with
`anchor_degraded: true` / `anchor_degraded_reason: "evidence_not_in_diff"`
(see ``tests/test_review_findings.py``). These tests pin the prompt half: the
coordinating session must re-anchor and bucket-sort such a finding on its
merits, a MUST_FIX that cannot be re-anchored is REJECTed *with a recorded
rationale* rather than escalated, the #1714 `review_blocked` exit survives for
the reasons that are still mechanically rejected, and reviewers are told to
quote the inlined diff rather than the on-disk file.
"""

from tests.conftest import _cmd
from tests.test_auto_dev_preflight_resolutions import _after, _nearby

DEGRADED_BULLET_ANCHOR = (
    "**An `.accepted` finding with `anchor_degraded: true` is a normal bucket "
    "candidate whose citation validation could not verify.**"
)
EVIDENCE_REASON_ANCHOR = '`"evidence_not_in_diff"` (#2099):'
FAILED_REANCHOR_ANCHOR = (
    '**If re-anchoring an `"evidence_not_in_diff"` MUST_FIX fails**'
)
REJECTED_MUST_FIX_ANCHOR = (
    "**A `.rejected` finding with `severity: MUST_FIX` may NOT be silently "
    "discarded** (#1714)."
)
EVIDENCE_CONTRACT_ANCHOR = (
    "`evidence` MUST be a verbatim substring of the diff text at the claimed lines"
)


def _review_doc() -> str:
    return _cmd("auto-dev-review.md")


def test_degraded_bullet_covers_both_carried_reasons() -> None:
    """The flag alone is ambiguous: the bullet must name the reason field."""
    window = _after(_review_doc(), DEGRADED_BULLET_ANCHOR, span=1600)
    assert "`anchor_degraded_reason`" in window
    assert '`"line_anchor_degraded"` (#2081):' in window
    assert EVIDENCE_REASON_ANCHOR in window


def test_degraded_finding_is_never_rejected_for_its_citation() -> None:
    window = _after(_review_doc(), DEGRADED_BULLET_ANCHOR, span=600)
    assert "Never REJECT one *for* its citation" in window
    assert "never treat it as a finding the reviewer filed at file level" in window


def test_unmatched_evidence_finding_must_be_reanchored_and_bucket_sorted() -> None:
    """The core rule: judge it on its merits, do not discard or escalate it."""
    window = _after(_review_doc(), EVIDENCE_REASON_ANCHOR, span=1200)
    assert "Re-anchor it yourself from `summary`/`evidence`" in window
    assert "bucket-sort it on its merits like any other finding" in window
    assert "never dropped and never an automatic operator escalation" in window


def test_unmatched_evidence_bullet_states_the_line_anchor_is_trustworthy() -> None:
    """Its endpoints DID resolve — the distinction from #2081's routing."""
    window = _after(_review_doc(), EVIDENCE_REASON_ANCHOR, span=1200)
    assert "the cited line DID resolve" in window
    assert "are trustworthy" in window


def test_unmatched_evidence_bullet_names_the_formatter_hook_cause() -> None:
    window = _after(_review_doc(), EVIDENCE_REASON_ANCHOR, span=1200)
    assert "formatter hook" in window
    assert "only its quote is stale" in window


def test_failed_reanchor_of_a_must_fix_goes_to_reject_with_a_rationale() -> None:
    """The claim is recorded in ADJUDICATIONS, never silently lost."""
    window = _after(_review_doc(), FAILED_REANCHOR_ANCHOR, span=1000)
    assert "**REJECT (bucket 2)**" in window
    assert "quoting the unmatched `evidence` verbatim" in window
    assert "`ADJUDICATIONS`" in window


def test_failed_reanchor_of_a_must_fix_does_not_exit_the_run() -> None:
    """It is not an operator escalation: the #1714 exit stays out of it."""
    window = _after(_review_doc(), FAILED_REANCHOR_ANCHOR, span=1000)
    assert "it does NOT exit the run" in window
    assert "Do not route it to the `review_blocked` exit" in window


def test_review_blocked_exit_survives_for_still_rejected_reasons() -> None:
    """#1714's exit is preserved, just narrowed to the reasons that remain."""
    window = _after(_review_doc(), REJECTED_MUST_FIX_ANCHOR, span=900)
    assert 'EXITS `blocked` with `blocker.reason: "review_blocked"`' in window
    assert (
        "a file or line that does not exist, a payload that failed its own schema"
    ) in window


def test_review_blocked_exit_no_longer_cites_an_absent_evidence_quote() -> None:
    """That reason left the rejected set — citing it would send an adjudicable
    finding to the operator park the fix exists to stop."""
    window = _after(_review_doc(), REJECTED_MUST_FIX_ANCHOR, span=900)
    assert "evidence quote absent from the diff" not in window


def test_rejected_discard_bullet_states_the_narrowed_reason_set() -> None:
    window = _nearby(_review_doc(), REJECTED_MUST_FIX_ANCHOR, span=900)
    assert "since #2099 an unmatched *evidence quote* is not a rejection reason" in (
        window
    )
    assert "arrives in `.accepted` flagged" in window


def test_reviewers_are_told_to_quote_the_inlined_diff_not_the_file() -> None:
    window = _after(_review_doc(), EVIDENCE_CONTRACT_ANCHOR, span=1400)
    assert (
        "**Quote it from the inlined diff you were given, never from the file "
        "on disk (#2099).**"
    ) in window
    assert "`PostToolUse` formatter hook" in window


def test_reviewer_evidence_rule_ties_to_the_shared_worktree_hazard() -> None:
    """#2087's rule and this one are the same hazard on two axes."""
    window = _after(_review_doc(), EVIDENCE_CONTRACT_ANCHOR, span=1400)
    assert "#2087" in window
    assert "on the evidence axis rather than the mutation axis" in window
