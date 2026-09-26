"""Guard tests: plan approval survives advisory-only draft drift with no
operator dissent (#2433).

`plan_approved_fingerprint` binds an approval to the exact draft text the
operator read (#2102), which is correct when the draft actually changed. But a
resumed round's own advisory re-scan can rewrite non-substantive sections of
the draft with no operator ever weighing in, producing a non-null,
non-matching `plan_approved_fingerprint` that re-parks a plan nobody actually
disputed. This adds a stage-agnostic "operator-authority delta" rule
(`.claude/commands/auto-dev.md`) — no live-fetched operator-authority comment
newer than the row's last durable park/approval timestamp — and wires it into
Checkpoint 1's row-path evidence, Step 1a.0b's fingerprint fast path, and the
fingerprint-mismatch sub-case as an alternate, fingerprint-equality-independent
sufficient condition.

Pure-markdown assertions over the auto-dev pipeline instruction files,
following the ``read_text()`` + literal-substring/window convention of
``test_plan_resolutions_delta_fast_path.py`` /
``test_plan_approval_fingerprint_binding.py`` /
``test_agent_comment_provenance.py``. ``_cmd``/``_appendix``/``_checkpoint1_section``
are imported from ``tests.conftest``; ``_after``/``_nearby`` from
``test_auto_dev_preflight_resolutions``; ``_rule_section``/``RULE_ANCHOR`` from
``test_agent_comment_provenance`` — no third private copy of any of these.

Windows are whitespace-normalized (``_norm``) before asserting multi-word
phrases, since the source prose hard-wraps at ~80-100 cols and a literal
line-broken assertion would be a hostage to reflow.
"""

from tests.conftest import _REPO_ROOT, _appendix, _checkpoint1_section, _cmd
from tests.test_agent_comment_provenance import RULE_ANCHOR, _rule_section
from tests.test_auto_dev_preflight_resolutions import _after, _nearby

NEW_SUBSECTION_ANCHOR = "### Operator-authority delta (#2433)"
DESTRUCTIVE_GATE_ANCHOR = "### Destructive-directive gate"
PROVENANCE_ANCHOR = "### Provenance — what carries operator authority"
FAST_PATH_BRANCH_ANCHOR = "**Operator-authority delta match → fast path (#2433).**"


def _norm(text: str) -> str:
    return " ".join(text.split())


def _headless_contract() -> str:
    return (_REPO_ROOT / "docs" / "headless-contract.md").read_text(encoding="utf-8")


def _reason_field_row() -> str:
    content = _headless_contract()
    return _norm(_after(content, "### 10.3 Payload Schema", span=2600))


# ---------------------------------------------------------------------------
# The new stage-agnostic rule in auto-dev.md's Comment provenance rule
# ---------------------------------------------------------------------------


def test_operator_authority_delta_subsection_exists_in_provenance_rule() -> None:
    """The new subsection sits between Provenance and the Destructive gate."""
    section = _rule_section()
    assert RULE_ANCHOR in section
    idx_provenance = section.index(PROVENANCE_ANCHOR)
    idx_new = section.index(NEW_SUBSECTION_ANCHOR)
    idx_gate = section.index(DESTRUCTIVE_GATE_ANCHOR)
    assert idx_provenance < idx_new < idx_gate


def test_operator_authority_delta_defined_generically_not_plan_stage_only() -> None:
    """The definition names a generic anchor T, not `plan_approved_at` alone."""
    window = _norm(_after(_rule_section(), NEW_SUBSECTION_ANCHOR, span=900))
    assert "`plan_approved_at` for the plan stage today" in window
    assert (
        "any future caller anchors T to its own stage's equivalent durable "
        "timestamp" in window
    )
    assert "never hardcoded to `plan_approved_at` as the only named anchor" in window


def test_operator_authority_delta_documents_comment_only_scope() -> None:
    """The subsection documents the body-edit limitation rather than omitting it."""
    window = _norm(_after(_rule_section(), NEW_SUBSECTION_ANCHOR, span=1800))
    assert "**Comment-scoped only.**" in window
    assert "This rule covers tracker comments alone" in window
    assert "body** edit since T is a documented, out-of-scope limitation" in window
    assert "`body_sha` tracker-state fingerprint" in window


def test_operator_authority_delta_requires_no_delta_not_mere_silence() -> None:
    """Both branches are stated explicitly; silence alone never suffices."""
    window = _norm(_after(_rule_section(), NEW_SUBSECTION_ANCHOR, span=1600))
    assert "never satisfied by silence alone" in window
    assert "**Delta absent.**" in window
    assert (
        "A durable approval/park timestamp T exists on the row AND no "
        "operator-authority comment postdates it" in window
    )
    assert "**Delta present.**" in window
    assert "unconditionally, regardless of how much time has passed" in window
    assert "absence of a T is absence of evidence, not evidence of no delta" in (window)


def test_operator_authority_delta_cites_impl_stage_followup_2438() -> None:
    """Impl-stage reuse is deferred to #2438; auto-dev-impl.md is untouched."""
    window = _norm(_after(_rule_section(), NEW_SUBSECTION_ANCHOR, span=600))
    assert "#2438" in window
    assert "`auto-dev-impl.md` is untouched by #2433" in window


# ---------------------------------------------------------------------------
# Checkpoint 1's row-path evidence gains the alternate sufficient condition
# ---------------------------------------------------------------------------


def test_checkpoint1_row_path_gains_operator_authority_alternate() -> None:
    """The alternate condition is named and does not require fingerprint equality."""
    section = _norm(_checkpoint1_section())
    assert (
        "Row-path alternate sufficient condition (operator-authority delta, "
        "#2433)" in section
    )
    assert (
        "independently sufficient without requiring `plan_approved_fingerprint` "
        "to equal `draft_fp`" in section
    )


def test_checkpoint1_existing_equality_language_still_present() -> None:
    """Regression pin: the #2102 equality sentence is untouched, not replaced."""
    section = _checkpoint1_section()
    assert "equal to `draft_fp`" in section
    assert (
        "A non-null timestamp is **not** sufficient by itself: it records that "
        "an approval happened, never which text the operator read" in section
    )


def test_checkpoint1_revocation_marker_gates_both_row_path_subconditions() -> None:
    """The revocation marker voids BOTH row-path sub-conditions, not just equality."""
    section = _checkpoint1_section()
    window = _norm(_after(section, "Before accepting this pair", span=700))
    assert "the alternate sufficient condition below" in window
    assert (
        "revoked and absent for both sub-conditions, not just the equality one"
        in window
    )
    assert "supersedes that marker for both" in window
    # The new alternate-condition paragraph itself restates the same guarantee.
    alt_window = _norm(
        _after(
            section,
            "Row-path alternate sufficient condition (operator-authority "
            "delta, #2433).",
            span=900,
        )
    )
    assert "already gates this alternate condition too" in alt_window
    assert (
        "cannot fire on a just-revoked, stale-cached `plan_approved_at` either"
        in alt_window
    )


# ---------------------------------------------------------------------------
# Fingerprint-mismatch sub-case checks the operator-authority-delta branch first
# ---------------------------------------------------------------------------


def test_fingerprint_mismatch_subcase_checks_operator_authority_delta_first() -> None:
    """The no-delta condition is checked before the unconditional fallback fires,
    and the quote-both-fingerprints language survives for that fallback case."""
    window = _norm(
        _after(
            _checkpoint1_section(),
            "**Fingerprint mismatch sub-case (#2102).**",
            span=1200,
        )
    )
    assert "Check the operator-authority-delta branch first (#2433)" in window
    assert "the approval transfers via that branch instead" in window
    assert "Only when that condition also fails" in window
    assert "quote both fingerprints" in window
    assert "does not transfer by fingerprint equality alone" in window


# ---------------------------------------------------------------------------
# Step 1a.0b's fingerprint fast-path check gains the new OR-branch
# ---------------------------------------------------------------------------


def test_step1a0b_fast_path_gains_operator_authority_branch() -> None:
    """The new branch cites the Operator-authority delta rule by name, not restated."""
    window = _norm(_after(_appendix("plan"), FAST_PATH_BRANCH_ANCHOR, span=700))
    assert (
        "per the *Operator-authority delta* rule (`.claude/commands/auto-dev.md`)"
        in window
    )
    assert "skip Step 1c's ambiguity/premise re-scan AND Step 1c.0's" in window
    # Not re-derived: the two-branch silence/no-delta prose is absent here.
    assert "never satisfied by silence alone" not in window


def test_step1a0b_new_reason_value_distinct_from_existing() -> None:
    """Two distinct `reason` literals: the new branch's and the equality branch's."""
    appendix = _appendix("plan")
    assert '\\"reason\\":\\"approved_fingerprint_match\\"' in appendix
    assert '\\"reason\\":\\"operator_approval_no_delta\\"' in appendix


def test_new_branch_never_emits_resolution_consumed_keys() -> None:
    """Regression guard tied to the productivity-ceiling touch-point (#1750):
    the new branch's own emitted payload never carries either key, and the
    prose says so explicitly."""
    window = _after(_appendix("plan"), FAST_PATH_BRANCH_ANCHOR, span=1300)
    payload_start = window.index('--payload "{')
    payload_end = window.index('}"', payload_start)
    payload = window[payload_start:payload_end]
    assert "resolution_consumed" not in payload
    assert "resolution_evidence" not in payload
    assert "never emits `resolution_consumed`/`resolution_evidence` either" in _norm(
        window
    )


def test_scope_note_on_evidence_source_unchanged() -> None:
    """Regression pin: the appendix's row-path-only scope note survives untouched."""
    window = _norm(
        _after(_appendix("plan"), "**Scope note on evidence source.**", span=500)
    )
    assert "scoped to the row-path only" in window
    assert "A comment-path-token-approved draft is unaffected by this section" in (
        window
    )


def test_step1a0b_branch_disqualified_by_newer_operator_comment() -> None:
    """The new branch falls through when an operator-authority comment postdates
    the park, mirroring the rule's own delta-present branch."""
    window = _norm(_after(_appendix("plan"), FAST_PATH_BRANCH_ANCHOR, span=1300))
    assert (
        "Any operator-authority comment newer than `plan_approved_at` "
        "disqualifies this branch and falls through to the next bullet" in window
    )


# ---------------------------------------------------------------------------
# docs/headless-contract.md §10.3 documents the new reason value
# ---------------------------------------------------------------------------


def test_headless_contract_reason_enum_documents_new_value() -> None:
    """The new value is named alongside the existing one, and the open-enum
    sentence is preserved verbatim."""
    window = _reason_field_row()
    assert "`reason` (str) — `s1_ambiguity_scan_skipped` only (#2376)." in window
    assert "`approved_fingerprint_match`" in window
    assert "`operator_approval_no_delta`" in window
    assert (
        "Open enum — consumers MUST tolerate unknown future values, mirroring "
        "`error_kind` below." in window
    )


def test_reason_row_still_singular_field_row() -> None:
    """The two values share the one `reason` bullet — no duplicate row added."""
    content = _headless_contract()
    assert content.count("- `reason` (str) — `s1_ambiguity_scan_skipped` only") == 1


def test_shared_helpers_resolve() -> None:
    """Guards against a stale import surviving a future rename of any shared
    reader this file depends on."""
    assert _cmd("auto-dev.md")
    assert _appendix("plan")
    assert _checkpoint1_section()
    assert _nearby(_cmd("auto-dev-plan.md"), "Row-path alternate sufficient condition")
