"""The codex PLAN revision turn and its Working-plan validation (#3464).

A revision is a second read-only ``codex exec`` turn through
:meth:`cw.codex_plan._run._Run.turn` with its own strict
:class:`~cw.codex_plan._result.ReviseTurnDocument` schema; this module never
launches a runner itself. Two callers use it: the resume gate's resolutions
revision (Step 1a.0b) and the Plan Reviewer MUST_FIX revision (Step 1f.4).

cw validates the returned text with the *Working-plan rule*: non-empty, no
leading ``plan-stage-`` line, the required headings present, every
accumulator section the prior draft carried still carried, and the
``## Self-Verified Premises`` entries byte-for-byte. On any failure the
prior draft is kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

from cw.codex_plan._exits import _Exit
from cw.codex_plan._result import ReviseTurnDocument, clean_plan_text
from cw.codex_review import _is_model_capacity_error
from cw.openai_strict_schema import to_openai_strict_schema

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.codex_plan._run import _McpTarget, _Run

#: The strict ``-o`` schema the revision turn answers with.
REVISE_TURN_SCHEMA: Final[dict[str, Any]] = to_openai_strict_schema(
    ReviseTurnDocument.model_json_schema()
)

#: The revision prompt's first line. It never contains the PLAN addendum
#: header, so a revision turn is never mistaken for a scan turn.
REVISION_HEADER: Final = "Codex PLAN revision turn"

REQUIRED_HEADINGS: Final[tuple[str, ...]] = (
    "## Patterns Found",
    "## Touch-point Contract",
    "## Files Modified",
    "## Non-Goals",
    "## Ambiguities",
)
ACCUMULATOR_HEADINGS: Final[tuple[str, ...]] = (
    "## Adopted Assumptions",
    "## Self-Verified Premises",
    "## Deferred Premises",
    "## Settled Plan Items",
)
CONFORMANCE_HEADING: Final = "## Pre-flight Resolution Conformance"
_SELF_VERIFIED: Final = "## Self-Verified Premises"
_AMBIGUITIES: Final = "## Ambiguities"
_BOOKKEEPING_PREFIX: Final = "<!-- plan-stage-"


def _headings(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines() if line.startswith("## ")]


def _section_lines(text: str, heading: str) -> list[str]:
    """The ``- `` entry lines under *heading* (until the next ``## ``)."""
    entries: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line.rstrip() == heading
            continue
        if inside and line.startswith("- "):
            entries.append(line)
    return entries


def _conformance_rejection(headings: list[str]) -> str | None:
    if CONFORMANCE_HEADING not in headings:
        return f"missing {CONFORMANCE_HEADING}"
    before = headings[headings.index(_AMBIGUITIES) - 1]
    if before != CONFORMANCE_HEADING:
        return f"{CONFORMANCE_HEADING} is not immediately before {_AMBIGUITIES}"
    return None


def check_working_plan(prior: str, revised: str, *, resolutions: bool) -> str | None:
    """Why *revised* fails the Working-plan rule against *prior*; ``None`` if valid.

    *resolutions* (a binding resolutions source exists) also requires
    ``## Pre-flight Resolution Conformance`` immediately before
    ``## Ambiguities``.
    """
    if not revised.strip():
        return "the revision is empty"
    if revised.lstrip().startswith(_BOOKKEEPING_PREFIX):
        return "the revision starts with a plan-stage- line"
    headings = _headings(revised)
    missing = [h for h in REQUIRED_HEADINGS if h not in headings]
    carried = [h for h in ACCUMULATOR_HEADINGS if h in _headings(prior)]
    missing.extend(h for h in carried if h not in headings)
    if missing:
        return f"missing {', '.join(missing)}"
    if resolutions:
        rejection = _conformance_rejection(headings)
        if rejection is not None:
            return rejection
    if _section_lines(prior, _SELF_VERIFIED) != _section_lines(revised, _SELF_VERIFIED):
        return f"{_SELF_VERIFIED} entries changed"
    return None


def render_revision_prompt(
    *,
    body: str,
    findings: Sequence[str] = (),
    resolutions: str | None = None,
) -> str:
    """The revision turn's stdin: the draft, what to address, and the rules."""
    parts = [
        f"{REVISION_HEADER}.\n\n"
        "You revise one plan. You write no files and run no state-changing "
        "command; cw validates and writes your answer. Return one JSON object "
        "with one string field, plan_markdown: the whole revised plan body, "
        "with no leading plan-stage- bookkeeping line and no sign-off marker.\n",
        "Keep every one of these headings: "
        + ", ".join((*REQUIRED_HEADINGS, *ACCUMULATOR_HEADINGS))
        + " (the last four only when the draft has them). Carry every "
        f"{_SELF_VERIFIED} entry byte-for-byte.\n",
    ]
    if resolutions is not None:
        parts.append(
            "## Binding Pre-flight Resolutions\n\n"
            f"{resolutions.rstrip()}\n\n"
            "Apply every resolution above in this pass and re-emit "
            f"{CONFORMANCE_HEADING} immediately before {_AMBIGUITIES}.\n"
        )
    if findings:
        parts.append(
            "## Review findings to address (MUST_FIX)\n\n" + "\n".join(findings) + "\n"
        )
    parts.append(f"## Current draft\n\n{body.rstrip()}\n")
    return "\n".join(parts)


@dataclass(frozen=True)
class RevisionOutcome:
    """A revision turn's result: the validated body, or why there is none.

    ``capacity`` marks a provider capacity failure (retryable); ``exit`` is a
    cw-built exit the turn itself returned (a tracker-MCP argv refusal).
    """

    text: str | None
    reason: str = ""
    capacity: bool = False
    exit: _Exit | None = None


def _parse(content: str | None) -> str | None:
    if content is None or not content.strip():
        return None
    try:
        return ReviseTurnDocument.model_validate_json(content).plan_markdown
    except ValidationError:
        return None


def revise_plan(
    run: _Run,
    target: _McpTarget,
    *,
    body: str,
    findings: Sequence[str] = (),
    resolutions: str | None = None,
) -> RevisionOutcome:
    """Run one revision turn on *body* and validate it (never raises on output)."""
    prompt = render_revision_prompt(
        body=body, findings=findings, resolutions=resolutions
    )
    turn = run.turn(prompt, target, schema=REVISE_TURN_SCHEMA)
    if isinstance(turn, _Exit):
        return RevisionOutcome(None, "the revision turn could not launch", exit=turn)
    if turn.returncode != 0 or turn.timed_out:
        if _is_model_capacity_error(turn):
            return RevisionOutcome(None, "model capacity", capacity=True)
        return RevisionOutcome(None, f"the revision turn exited {turn.returncode}")
    revised = _parse(turn.output_file_content)
    if revised is None:
        return RevisionOutcome(None, "the revision turn wrote no valid document")
    rejection = check_working_plan(body, revised, resolutions=resolutions is not None)
    if rejection is not None:
        return RevisionOutcome(None, rejection)
    return RevisionOutcome(clean_plan_text(revised))


__all__ = [
    "REQUIRED_HEADINGS",
    "REVISE_TURN_SCHEMA",
    "REVISION_HEADER",
    "RevisionOutcome",
    "check_working_plan",
    "render_revision_prompt",
    "revise_plan",
]
