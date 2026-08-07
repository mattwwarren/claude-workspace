"""Guard tests: headless re-dispatch enters via stage detection (#1652).

Pure-markdown assertions over ``auto-dev.md``, following the ``read_text()``
+ literal-substring/window convention of ``test_auto_dev_preflight_resolutions.py``.

Background: auto-dev defines a durable-signal stage detector, but without
explicit ``--resume`` it was informational only — a headless queue re-dispatch
always re-entered at Stage 1, even against a branch whose implementation had
already shipped (observed: a full Stage-1 re-run that re-verified merged code
and re-posted an already-open question). Headless invocations now always run
the detector first and, when the durable signals are unambiguous, enter at the
latest detected stage — identical to explicit ``--resume``.
"""

from pathlib import Path

from tests.test_auto_dev_preflight_resolutions import _after

ROOT = Path(__file__).parent.parent
COMMANDS = ROOT / ".claude" / "commands"

SECTION_ANCHOR = "### Headless implicit resume (#1652)"


def _auto_dev() -> str:
    return (COMMANDS / "auto-dev.md").read_text()


def _implicit_resume_section() -> str:
    content = _auto_dev()
    start = content.index(SECTION_ANCHOR)
    end = content.index("### Durable signals")
    return content[start:end]


def test_headless_always_runs_detector_first() -> None:
    """Every headless run enters via the detector, with or without --resume."""
    section = _implicit_resume_section()
    assert "Headless invocations always enter via the detector." in section
    assert "including every queue re-dispatch" in section
    assert "identical to explicit `--resume`" in section


def test_implicit_resume_emits_resumed_from_stage() -> None:
    """An implicit jump emits the same resumed_from_stage field as --resume."""
    section = _implicit_resume_section()
    assert 'resumed_from_stage: "<stage>"' in section


def test_ambiguous_signals_fall_back_to_stage_1() -> None:
    """Signal conflict or ambiguity falls back to Stage 1, never a wrong jump."""
    section = _implicit_resume_section()
    assert "fall back to Stage 1 as today" in section
    assert "Ambiguity favors the conservative start" in section
    # The issue's two canonical ambiguity examples are documented.
    assert "plan markers are absent or stale" in section
    assert "no pipeline trailers" in section


def test_implicit_resume_preserves_stage_entry_gates() -> None:
    """Implicit resume changes where the pipeline starts, not what it may skip."""
    section = _implicit_resume_section()
    assert "Stage-entry gates are unchanged." in section
    assert "never what it may skip within a stage" in section


def test_interactive_without_resume_stays_informational() -> None:
    """Interactive mode without --resume keeps the detector informational."""
    section = _implicit_resume_section()
    assert "Interactive mode without `--resume` keeps current behavior" in section
    content = _auto_dev()
    assert (
        "In **interactive mode** without `--resume`, the detector still runs "
        "but is informational" in content
    )


def test_decision_table_has_implicit_resume_row() -> None:
    """The headless decision table carries the implicit-resume row."""
    content = _auto_dev()
    window = _after(
        content,
        "| Headless invocation without `--resume` (every queue re-dispatch) |",
        span=400,
    )
    assert "Implicit resume (#1652)" in window
    assert "conflicting/ambiguous signals → enter Stage 1" in window
