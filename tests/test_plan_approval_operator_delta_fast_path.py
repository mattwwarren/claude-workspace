"""Guard tests for the operator-authority-delta fast path (#2433).

These are pure-markdown assertions over the plan-stage instructions and
headless contract. Shared readers are imported from the established test
helpers rather than duplicated here.
"""

from tests.conftest import _REPO_ROOT, _appendix, _checkpoint1_section
from tests.test_agent_comment_provenance import RULE_ANCHOR, _rule_section
from tests.test_auto_dev_preflight_resolutions import _after

NEW_SUBSECTION_ANCHOR = "### Operator-authority delta (#2433)"
DESTRUCTIVE_GATE_ANCHOR = "### Destructive-directive gate"
PROVENANCE_ANCHOR = "### Provenance — what carries operator authority"


def _norm(text: str) -> str:
    return " ".join(text.split())


def _headless_contract() -> str:
    return (_REPO_ROOT / "docs" / "headless-contract.md").read_text(encoding="utf-8")


def _reason_field_row() -> str:
    return _norm(_after(_headless_contract(), "### 10.3 Payload Schema", span=2600))


def test_operator_authority_delta_subsection_exists_in_provenance_rule() -> None:
    section = _rule_section()
    assert RULE_ANCHOR in section
    assert section.index(PROVENANCE_ANCHOR) < section.index(NEW_SUBSECTION_ANCHOR)
    assert section.index(NEW_SUBSECTION_ANCHOR) < section.index(DESTRUCTIVE_GATE_ANCHOR)


def test_operator_authority_delta_defined_generically_not_plan_stage_only() -> None:
    window = _norm(_after(_rule_section(), NEW_SUBSECTION_ANCHOR, span=900))
    assert "`plan_approved_at` for the plan stage today" in window
    assert (
        "any future caller anchors T to its own stage's equivalent durable timestamp"
        in window
    )
    assert "never hardcoded to `plan_approved_at` as the only named anchor" in window


def test_operator_authority_delta_documents_comment_only_scope() -> None:
    window = _norm(_after(_rule_section(), NEW_SUBSECTION_ANCHOR, span=1800))
    assert "**Comment-scoped only.**" in window
    assert "body** edit since T is a documented, out-of-scope limitation" in window
    assert "`body_sha` tracker-state fingerprint" in window


def test_checkpoint1_row_path_gains_operator_authority_alternate() -> None:
    section = _norm(_checkpoint1_section())
    assert "either (a)" in section
    assert "no operator-authority delta since `plan_approved_at`" in section
    assert "latter branch does not require fingerprint equality" in section


def test_checkpoint1_existing_equality_language_still_present() -> None:
    assert "equal to `draft_fp`" in _checkpoint1_section()


def test_checkpoint1_no_delta_alternate_still_requires_fingerprint() -> None:
    """A timestamp without the paired durable fingerprint is not approval."""
    section = _norm(_checkpoint1_section())
    row_path = _after(section, "**Row path:**", span=1500)
    assert (
        "non-null `queue_metadata.plan_approved_at` **AND** non-null "
        "`queue_metadata.plan_approved_fingerprint`"
    ) in row_path
    assert (
        "tolerates a non-equal fingerprint but still requires both durable approval "
        "fields"
    ) in row_path


def test_step1a0b_fast_path_gains_operator_authority_branch() -> None:
    window = _norm(_after(_appendix("plan"), "Fingerprint fast-path check", span=2600))
    assert "Operator-authority-delta alternate" in window
    assert "Operator-authority delta" in window
    assert "independently of fingerprint equality" in window


def test_step1a0b_new_reason_value_distinct_from_existing() -> None:
    appendix = _appendix("plan")
    assert '\\"reason\\":\\"approved_fingerprint_match\\"' in appendix
    assert '\\"reason\\":\\"operator_approval_no_delta\\"' in appendix


def test_headless_contract_reason_enum_documents_new_value() -> None:
    window = _reason_field_row()
    assert "`operator_approval_no_delta`" in window
    assert "Open enum — consumers MUST tolerate unknown future values" in window


def test_new_branch_never_emits_resolution_consumed_keys() -> None:
    window = _norm(
        _after(_appendix("plan"), "Operator-authority-delta alternate", span=1400)
    )
    marker = "never emits `resolution_consumed` or `resolution_evidence`"
    assert marker in window
    assert window.count("`resolution_consumed`") == 1
    assert window.count("`resolution_evidence`") == 1


def test_scope_note_on_evidence_source_unchanged() -> None:
    window = _norm(
        _after(_appendix("plan"), "**Scope note on evidence source.**", span=700)
    )
    assert (
        "existing fingerprint-equality evidence check is scoped to the row-path only"
        in window
    )
    assert "comment-path-token-approved draft is unaffected" in window


def test_step1a0b_distinguishes_resolutions_changes_from_ordinary_body_edits() -> None:
    window = _norm(
        _after(
            _appendix("plan"),
            "Because step 3 (delta/revision) runs strictly before step 4",
            span=1100,
        )
    )
    assert "body edit that changes the resolutions source" in window
    assert (
        "ordinary ticket body edit that does not change the resolutions source does "
        "not"
    ) in window
    assert "that rule is comment-scoped only" in window


def test_operator_authority_delta_requires_no_delta_not_mere_silence() -> None:
    window = _norm(_after(_rule_section(), NEW_SUBSECTION_ANCHOR, span=1600))
    assert "never satisfied by silence alone" in window
    assert (
        "A durable approval/park timestamp T exists on the row AND no "
        "operator-authority comment postdates it"
        in window
    )
    assert "Any operator-authority comment newer than T" in window
    assert "unconditionally, regardless of how much time has passed" in window
    assert "absence of a T is absence of evidence, not evidence of no delta" in window


def test_checkpoint1_revocation_marker_gates_both_row_path_subconditions() -> None:
    window = _norm(
        _after(
            _checkpoint1_section(),
            "Before accepting either row-path sub-condition",
            span=900,
        )
    )
    assert (
        "entire row-path evidence pair and both sub-conditions as revoked and absent"
        in window
    )
    assert "a later `plan_approved_at` is a fresh approval" in window


def test_fingerprint_mismatch_subcase_checks_operator_authority_delta_first() -> None:
    window = _norm(
        _after(
            _checkpoint1_section(),
            "**Fingerprint mismatch sub-case (#2102).**",
            span=1800,
        )
    )
    assert "First evaluate the *Operator-authority delta* alternate" in window
    assert "If that no-delta condition does not hold" in window
    assert "quote both fingerprints" in window
    assert (
        "A resumed draft classified **Small** follows the ordinary AUTO-SKIP path"
        in window
    )
    assert "**always** EXIT `plan_pending_approval`" not in window
