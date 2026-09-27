"""Guard tests: impl-stage re-park must honor the operator-authority delta (#2438).

The Pre-Stage Detector Guard's staleness/regress check (#1794) only ran when
the detector reported a stage past S2 -- never for `s2_implementing`. A
merge-conflict BLOCK during IMPL never reaches the completion trailer, so a
ticket regressed/requeued to IMPL while still `s2_implementing` skipped the
check entirely: the resuming agent was told only to "continue on top of
existing commits," with nothing handing it an operator's resolution comment.
These tests pin the widened trigger and the new stale=true sub-case that
applies the *Operator-authority delta* rule (`auto-dev.md`, #2433) to the impl
stage, anchored to `HEAD_COMMIT_AT` rather than `plan_approved_at`.
"""

from tests.conftest import _cmd
from tests.test_agent_comment_provenance import _rule_section
from tests.test_auto_dev_preflight_resolutions import _after
from tests.test_impl_guard_staleness_docs import _guard_section


def _norm(text: str) -> str:
    return " ".join(text.split())


def _s2_implementing_bullet() -> str:
    section = _guard_section()
    start = section.index('If `stage == "s2_implementing"`')
    end = section.index("- If `stage` is past S2", start)
    return section[start:end]


def _past_s2_stale_true_bullet() -> str:
    section = _guard_section()
    start = section.index("- If `stage` is past S2")
    end = section.index("\n\n---", start)
    return section[start:end]


def _core_guard_bullet() -> str:
    content = _cmd("auto-dev-impl.md")
    start = content.index("**Any other verdict**")
    end = content.index("**Headless only", start)
    return content[start:end]


def test_staleness_check_trigger_no_longer_past_s2_only() -> None:
    section = _guard_section()
    assert (
        "run before applying either bullet below, whenever the detector "
        "reports a stage past S2" not in _norm(section)
    )
    window = _after(section, "Staleness/regress check (#1794)", span=300)
    assert "every arrival at this section" in window
    assert "s2_implementing" in window


def test_s2_implementing_stale_false_path_unchanged() -> None:
    bullet = _s2_implementing_bullet()
    marker = "continue on top of existing commits"
    assert marker in bullet
    prefix = bullet[: bullet.index(marker)]
    assert "binding instructions" not in prefix
    assert "Operator-authority delta" not in prefix


def test_s2_implementing_stale_true_forces_fresh_attempt() -> None:
    bullet = _s2_implementing_bullet()
    assert "as new, binding instructions to read and act on" in bullet
    assert "Operator-authority delta" in bullet
    assert "HEAD_COMMIT_AT" in bullet
    assert 'do NOT treat this as "repo state unchanged' in bullet
    assert "silently re-park" in bullet


def test_past_s2_stale_true_also_forbids_deterministic_repark() -> None:
    bullet = _past_s2_stale_true_bullet()
    assert "as new, binding instructions to read and act on" in bullet
    assert "Operator-authority delta" in bullet
    assert "HEAD_COMMIT_AT" in bullet
    assert 'do NOT treat this as "repo state unchanged' in bullet
    assert "silently re-park" in bullet


def test_core_doc_bullet_drops_past_s2_only_restriction() -> None:
    bullet = _core_guard_bullet()
    assert "run before them whenever the detector reports a stage past S2" not in _norm(
        bullet
    )
    assert "s2_implementing" in bullet
    content = _cmd("auto-dev-impl.md")
    assert "run `detect_current_stage()`" in content
    assert "Pre-Stage Detector Guard: resume dispositions" in content
    assert "never re-implement over existing branch work by default" in content


def test_auto_dev_md_documents_impl_stage_consumption() -> None:
    section = _rule_section()
    idx_fastpath = section.index("Fast-path composition requires a third condition")
    idx_new = section.index("Impl-stage consumption (#2438)")
    assert idx_fastpath < idx_new
    window = _norm(_after(section, "Impl-stage consumption (#2438)", span=700))
    assert "HEAD_COMMIT_AT" in window
    assert (
        'section "Pre-Stage Detector Guard: resume dispositions and the '
        'staleness check (#1794)"' in window
    )
