"""Guard tests: plan approval survives draft drift with no
operator dissent (#2433).

`plan_approved_fingerprint` binds an approval to the exact draft text the
operator read (#2102). A resumed round may have no newer operator comment while
still carrying a changed draft, but the current contract has no durable
approved-draft snapshot or section-level digest that can machine-verify such a
change as advisory-only. The stage-agnostic "operator-authority delta" rule
(`.claude/commands/auto-dev.md`) therefore remains context only; a mismatched
fingerprint stays parked for fresh approval until such a verifier exists.

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
# Checkpoint 1's row-path evidence keeps the mismatch safety gate
# ---------------------------------------------------------------------------


def test_checkpoint1_mismatch_requires_machine_checkable_baseline() -> None:
    """Comment silence is not an alternate approval source for a mismatch."""
    section = _norm(_checkpoint1_section())
    assert "Fingerprint-mismatch safety gate (#2433)" in section
    assert "not independently sufficient to transfer approval" in section
    assert "only machine-checkable baseline currently available" in section
    assert "any mismatch remains absent row-path evidence" in section


def test_checkpoint1_mismatch_fails_closed_even_without_operator_comment() -> None:
    """A mismatch falls through even when the operator has been silent."""
    section = _norm(_checkpoint1_section())
    assert "including when no operator-authority comment postdates" in section
    assert "A substantive, destructive, or body change must never transfer" in section


def test_checkpoint1_existing_equality_language_still_present() -> None:
    """Regression pin: the #2102 equality sentence is untouched, not replaced."""
    section = _checkpoint1_section()
    assert "equal to `draft_fp`" in section
    assert (
        "A non-null timestamp is **not** sufficient by itself: it records that "
        "an approval happened, never which text the operator read" in section
    )


def test_checkpoint1_revocation_marker_gates_row_path_evidence() -> None:
    """The revocation marker voids the exact-equality row-path evidence."""
    section = _checkpoint1_section()
    window = _norm(_after(section, "Before accepting this pair", span=700))
    assert "treat the row evidence as revoked and absent" in window
    assert "a later `plan_approved_at` is a fresh approval" in window


# ---------------------------------------------------------------------------
# Fingerprint-mismatch sub-case checks the operator-authority-delta branch first
# ---------------------------------------------------------------------------


def test_fingerprint_mismatch_subcase_always_requires_fresh_approval() -> None:
    """The mismatch fallback remains unconditional and quotes both fingerprints."""
    window = _norm(
        _after(
            _checkpoint1_section(),
            "**Fingerprint mismatch sub-case (#2102).**",
            span=1200,
        )
    )
    assert "no machine-checkable advisory-only verification" in window
    assert (
        "even the absence of a newer operator-authority comment cannot transfer"
        in window
    )
    assert "**always** EXIT `plan_pending_approval`" in window
    assert "quote both fingerprints" in window


# ---------------------------------------------------------------------------
# Step 1a.0b's fingerprint fast-path keeps mismatches parked
# ---------------------------------------------------------------------------


def test_step1a0b_mismatch_requires_advisory_only_verification() -> None:
    """The appendix defines the executable baseline and fail-closed behavior."""
    window = _norm(
        _after(
            _appendix("plan"),
            "**Fingerprint-mismatch safety gate (#2433).**",
            span=900,
        )
    )
    assert "comment silence is not an advisory-only verification" in window
    assert "only machine-checkable baseline currently available" in window
    assert "must fall through to Step 1c.0 / Step 1c for fresh approval" in window


def test_step1a0b_has_no_mismatch_fast_path_reason() -> None:
    """Only exact fingerprint matches may emit the skip event."""
    appendix = _appendix("plan")
    assert '\\"reason\\":\\"approved_fingerprint_match\\"' in appendix
    assert "operator_approval_no_delta" not in appendix


def test_scope_note_on_evidence_source_unchanged() -> None:
    """Regression pin: the appendix's row-path-only scope note survives untouched."""
    window = _norm(
        _after(_appendix("plan"), "**Scope note on evidence source.**", span=500)
    )
    assert "scoped to the row-path only" in window
    assert "A comment-path-token-approved draft is unaffected by this section" in (
        window
    )


# ---------------------------------------------------------------------------
# docs/headless-contract.md §10.3 documents the surviving reason value
# ---------------------------------------------------------------------------


def test_headless_contract_reason_enum_documents_exact_match_only() -> None:
    """The mismatch path has no separate skip reason."""
    window = _reason_field_row()
    assert "`reason` (str) — `s1_ambiguity_scan_skipped` only (#2376)." in window
    assert "`approved_fingerprint_match`" in window
    assert "operator_approval_no_delta" not in window
    assert (
        "Open enum — consumers MUST tolerate unknown future values, mirroring "
        "`error_kind` below." in window
    )


def test_reason_row_still_singular_field_row() -> None:
    """The reason remains one open-enum field row."""
    content = _headless_contract()
    assert content.count("- `reason` (str) — `s1_ambiguity_scan_skipped` only") == 1


def test_shared_helpers_resolve() -> None:
    """Guards against a stale import surviving a future rename of any shared
    reader this file depends on."""
    assert _cmd("auto-dev.md")
    assert _appendix("plan")
    assert _checkpoint1_section()
    assert _nearby(_cmd("auto-dev-plan.md"), "Fingerprint-mismatch safety gate")
