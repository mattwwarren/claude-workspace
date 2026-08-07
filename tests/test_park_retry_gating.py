"""Guard tests: human-gated parks are not retry-eligible (#1653), skill layer.

The code layer (add_ticket parked-row dedup, wedge human-gated-park guards)
is covered in ``test_dev_queue.py`` and ``test_doctor.py``. This file guards
the operator-facing skill guidance, following the ``read_text()`` +
literal-substring convention of ``test_auto_dev_preflight_resolutions.py``.
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent
SKILLS = ROOT / ".claude" / "skills"


def _skill(name: str) -> str:
    return (SKILLS / name).read_text()


def test_orchestrate_sprint_declares_retry_eligibility_rule() -> None:
    """The triage section forbids re-dispatch without a tracker-state delta."""
    content = _skill("orchestrate-sprint/SKILL.md")
    assert (
        "Human-gated parks are not retry-eligible without a tracker-state delta"
        in content
    )
    for status in (
        "`ambiguities_pending_resolution`",
        "`premises_pending_verification`",
        "`plan_pending_approval`",
        "`review_pending_approval`",
    ):
        assert status in content


def test_orchestrate_sprint_names_the_delta_and_release_verbs() -> None:
    """The rule names what counts as a delta and the release verbs."""
    normalized = " ".join(_skill("orchestrate-sprint/SKILL.md").split())
    assert "a new operator comment, a body edit, or an approval reply" in normalized
    assert "`cw dev-queue requeue`/`approve`" in normalized
    assert "never by `cw dev-queue add`" in normalized
