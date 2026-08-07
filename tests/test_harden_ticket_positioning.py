"""Guard tests: harden-ticket repositioned as targeted pre-flight (#1655).

Pure-markdown assertions over the two skills, following the ``read_text()`` +
literal-substring convention of ``test_auto_dev_preflight_resolutions.py``.

Background: /harden-ticket front-loaded an operator round for every
non-trivial ticket to compensate for the pipeline surfacing findings one exit
class at a time. After consolidated park (#1650) and draft persistence
(#1649), round 1 of a dispatch produces the same comprehensive findings list
grounded in dispatch-time code, so the default flips to dispatch-first and
the skill keeps only its targeted cases.
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent
SKILLS = ROOT / ".claude" / "skills"


def _skill(name: str) -> str:
    return (SKILLS / name).read_text()


def _normalized(name: str) -> str:
    return " ".join(_skill(name).split())


def test_harden_default_is_dispatch_first() -> None:
    """The When-to-run section leads with the dispatch-first default."""
    content = _normalized("harden-ticket/SKILL.md")
    assert "Default for ordinary tickets: dispatch first." in content
    assert "Round 1's consolidated park IS the hardening sweep" in content


def test_harden_names_why_pipeline_sweep_wins() -> None:
    """The three advantages over pre-flight are documented (rot/nits/rounds)."""
    content = _normalized("harden-ticket/SKILL.md")
    assert "Grounded at dispatch time — no rot window." in content
    assert "No drafting-nit gap." in content
    assert "One comment, two rounds total." in content


def test_harden_keeps_four_targeted_cases() -> None:
    """All four mandatory pre-flight cases survive the repositioning."""
    content = _normalized("harden-ticket/SKILL.md")
    assert "Multi-task plan docs whose literal code the worker transcribes" in content
    assert "The pipeline cannot do that sweep for the plan doc itself." in content
    assert "Tickets defining public contracts" in content
    assert "Tickets already bouncing" in content
    assert "zero mid-wave interrupts" in content


def test_harden_reactive_trigger_unchanged() -> None:
    """Bouncing tickets still trigger a reactive harden."""
    content = _normalized("harden-ticket/SKILL.md")
    assert "don't just re-dispatch and hope" in content


def test_harden_description_reflects_targeted_default() -> None:
    """The skill's trigger description no longer claims every non-trivial ticket."""
    content = _normalized("harden-ticket/SKILL.md")
    assert "ordinary tickets converge fastest by dispatching first" in content
    assert "Use this whenever you are about to queue or dispatch" not in content


def test_orchestrate_sprint_phase2_flips_to_dispatch_first() -> None:
    """Phase 2 carries the same dispatch-first default."""
    content = _normalized("orchestrate-sprint/SKILL.md")
    assert "For ordinary tickets, **dispatch first**" in content
    assert "round 1's consolidated park (#1650) is the hardening sweep" in content


def test_orchestrate_sprint_phase2_keeps_read_fresh_comments_rule() -> None:
    """The existing rule-3 guidance (read fresh plan comments) is retained."""
    content = _normalized("orchestrate-sprint/SKILL.md")
    assert (
        "if the ticket already has fresh auto-dev plan comments, "
        "read them instead of re-sweeping" in content
    )
