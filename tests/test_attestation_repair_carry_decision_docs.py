"""Guard tests: a carried changelog repair does not re-pin the attestation (#3893).

Pure-markdown assertions in the style of ``test_confirm_spend_decision_docs.py``,
plus code-level pins. The decision is recorded in the dispatch runbook's
CHANGELOG-only conflict repair section, with cross-references in
``docs/headless-contract.md`` section 13.5 and ``docs/review-recipes-lessons.md``.
The code pins make "carry is ledger-only" falsifiable: if the attestation model
grows a carry field, or a repair recipe starts writing the attestation, the
decision has been reopened and must be superseded in the runbook rather than
silently contradicted.
"""

from __future__ import annotations

import re
from pathlib import Path

from cw.models.review_attestation import ReviewAttestation
from cw.review_convergence._models import CleanShaRemap, ConvergenceLedger
from tests._shared.workflow_text import _norm
from tests.test_auto_dev_preflight_resolutions import _after

ROOT = Path(__file__).resolve().parent.parent

RUNBOOK = "docs/dispatch-runbook.md"
CONTRACT = "docs/headless-contract.md"
LESSONS = "docs/review-recipes-lessons.md"
RECIPES_DIR = ROOT / "src" / "cw" / "reconcile" / "review_recipes"

RUNBOOK_SECTION = (
    "### CHANGELOG-only conflict repair (`repair_changelog_conflict`, #2771)"
)
RUNBOOK_ANCHOR = "**Attestation is not carried (#3893).**"
RUNBOOK_NEXT_SECTION_ITEM = "**Release an unarmed repair.**"
CONTRACT_13_5 = "### 13.5 Tolerated later commits"
CONTRACT_13_6 = "### 13.6"
LESSONS_HEADING = "## `repair_changelog_conflict` (#2771)"

RECIPE_WRITER_NAMES = (
    "record_review_attestation",
    "attest_review_exit",
    "refresh_deferral_tickets",
)
RECIPE_ATTESTATION_WRITE = re.compile(r"review_attestation[\"']?\s*(=[^=]|:)")
CARRY_FIELD_MARKERS = ("carr", "remap", "repin", "rebuilt")


def test_runbook_records_the_attestation_not_carried_decision() -> None:
    runbook = (ROOT / RUNBOOK).read_text(encoding="utf-8")
    section_at = runbook.index(RUNBOOK_SECTION)
    anchor_at = runbook.index(RUNBOOK_ANCHOR)
    next_item_at = runbook.index(RUNBOOK_NEXT_SECTION_ITEM)
    assert section_at < anchor_at < next_item_at
    text = _norm(_after(runbook, RUNBOOK_ANCHOR, span=4500))
    for needle in (
        "invalid:not_ancestor",
        "review.attestation.pr_section",
        "cosmetic",
        "clean_sha_remaps",
        "head_sha",
        "names a commit a review exited clean at",
        "public contract",
        "headless-contract.md",
        "verifier",
        "Reopen when",
        "stripped_invalid:not_ancestor",
        "finalize next renders",
    ):
        assert needle in text, f"runbook decision missing {needle!r}"


def test_runbook_rejected_alternatives_name_both_paths() -> None:
    runbook = (ROOT / RUNBOOK).read_text(encoding="utf-8")
    text = _norm(_after(runbook, RUNBOOK_ANCHOR, span=4500))
    for needle in ("Rejected:", "verifier-side", "rewrite the attestation"):
        assert needle in text, f"runbook rejected alternatives missing {needle!r}"


def test_contract_section_13_5_notes_a_rewritten_head() -> None:
    contract = (ROOT / CONTRACT).read_text(encoding="utf-8")
    start = contract.index(CONTRACT_13_5)
    end = contract.index(CONTRACT_13_6, start)
    text = _norm(contract[start:end])
    for needle in (
        "invalid:not_ancestor",
        "repair_changelog_conflict",
        "never moves",
        "dispatch-runbook.md",
    ):
        assert needle in text, f"headless-contract 13.5 missing {needle!r}"


def test_lessons_doc_points_at_the_decision() -> None:
    lessons = (ROOT / LESSONS).read_text(encoding="utf-8")
    start = lessons.index(LESSONS_HEADING)
    end = lessons.index("\n## ", start + len(LESSONS_HEADING))
    text = _norm(lessons[start:end])
    for needle in ("#3893", "dispatch-runbook.md"):
        assert needle in text, f"review-recipes-lessons missing {needle!r}"


def test_attestation_model_has_no_carry_field() -> None:
    offending = [
        name
        for name in ReviewAttestation.model_fields
        if any(marker in name for marker in CARRY_FIELD_MARKERS)
    ]
    assert not offending, (
        f"ReviewAttestation grew carry-like field(s) {offending}: the #3893 "
        "decision (no re-pin) is reopened. Supersede it in the runbook; do not "
        "silently contradict it."
    )


def test_no_review_recipe_writes_the_attestation() -> None:
    offending: list[str] = []
    for path in sorted(RECIPES_DIR.glob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            if any(name in line for name in RECIPE_WRITER_NAMES) or (
                RECIPE_ATTESTATION_WRITE.search(line)
            ):
                offending.append(f"{path.name}: {line.strip()}")
    assert not offending, (
        "a review recipe writes the review attestation, which the #3893 decision "
        "says it never does. Supersede the decision in "
        f"{RUNBOOK} rather than silently contradicting it: {offending}"
    )


def test_carry_audit_lives_in_the_ledger() -> None:
    assert {"from_sha", "to_sha", "commits"} <= set(CleanShaRemap.model_fields)
    assert "clean_sha_remaps" in ConvergenceLedger.model_fields
